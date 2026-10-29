"""Common finite daytime market-data runner, with no strategy or execution calls."""

import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from trading_system.brokers.locking import single_writer
from trading_system.runtime.models import daytime_deadline
from trading_system.storage.market_data import MarketDataStore
from trading_system.storage.store import atomic_write, canonical


def run(
    root,
    adapter_factory,
    *,
    max_seconds=None,
    interval=5,
    clock=None,
    sleep=None,
    recovery_probe=None,
):
    """Acquire lock before prompting; after 16:00 only recover, save and audit local data."""
    if interval < 5 or (max_seconds is not None and max_seconds <= 0):
        raise ValueError("invalid_capture_limits")
    clock = clock or (lambda: datetime.now(UTC))
    sleep = sleep or time.sleep
    started = clock()
    deadline = daytime_deadline(started)
    limit = min(deadline, started + timedelta(seconds=max_seconds)) if max_seconds else deadline
    run_id = uuid4().hex
    summary = {
        "schema_version": 1,
        "session_id": run_id,
        "started_at": started.isoformat(),
        "scheduled_stop_at": deadline.isoformat(),
        "capture_until": limit.isoformat(),
        "status": "starting",
        "connection_state": "offline",
        "samples": 0,
        "board_responses": 0,
        "accepted": 0,
        "gaps": 0,
        "recoveries": 0,
        "injected_failures": 0,
        "real_execution_enabled": False,
        "order_requests": 0,
        "normal_exit": False,
        "data_consistency_verified": False,
        "freshness_verified": False,
        "broker_restart_verified": False,
    }

    def status():
        summary["updated_at"] = clock().isoformat()
        atomic_write(root / "status.json", canonical(summary).encode())

    def stop_reason():
        if clock() >= deadline:
            return "daytime_1600"
        if clock() >= limit:
            return "duration_complete"
        if (root / "STOP").exists():
            return "operator_stop"
        return None

    with single_writer(root):
        store = MarketDataStore(root)
        adapter = None
        failed = False
        gap = None
        failures = 0
        try:
            status()
            store.recover()
            if not stop_reason():
                adapter = adapter_factory()
            if adapter is None:
                summary["stop_reason"] = stop_reason() or "prompt_cancelled"
            else:
                summary["status"] = "running"
                while not (reason := stop_reason()):
                    record, raw = adapter.refresh()
                    injected = bool(recovery_probe and recovery_probe.just_injected)
                    record["fault_injected"] = injected
                    summary["injected_failures"] += int(injected)
                    summary["samples"] += 1
                    record["session_id"] = run_id
                    store.save(run_id, summary["samples"], record, raw)
                    state = record["connection_state"]
                    summary["connection_state"] = state
                    summary["last_read_completed_at"] = record["read_completed_at"]
                    summary["last_http_status"] = record["http_status"]
                    summary["last_reason"] = record["reason"]
                    summary["last_issues"] = record["issues"]
                    summary["board_responses"] += int(
                        record["operation"] == "board" and record["http_status"] == 200
                    )
                    summary["accepted"] += int(record["accepted"])
                    if record["common_quote"]:
                        summary["last_quote_source_time"] = record["common_quote"][
                            "source_updated_at"
                        ]
                    if record["outcome"] != "ok":
                        failures += 1
                        if gap is None:
                            gap = record["read_started_at"]
                            summary["gaps"] += 1
                            summary["open_gap_since"] = gap
                    elif record["operation"] == "board":
                        failures = 0
                    if record["accepted"]:
                        # Exercise the existing common accessors only after successful persistence.
                        adapter.quote(record["code"], clock())
                        adapter.board(record["code"], clock())
                        summary["common_access_verified"] = True
                        if gap is not None:
                            summary["recoveries"] += 1
                            summary["last_recovered_at"] = record["read_completed_at"]
                            summary["open_gap_since"] = None
                            gap = None
                    if summary["samples"] % 12 == 0:
                        summary["last_verified_export"] = store.export()
                    status()
                    if (record["outcome"] != "ok" and not record["retryable"]) or failures >= 6:
                        summary["stop_reason"] = (
                            "retry_limit" if failures >= 6 else record["outcome"]
                        )
                        failed = True
                        break
                    wait = min(60, interval * 2 ** max(0, failures - 1))
                    if record["http_status"] == 429:
                        wait = max(30, wait)
                    until = min(limit, clock() + timedelta(seconds=wait))
                    while clock() < until and not stop_reason():
                        sleep(min(1, (until - clock()).total_seconds()))
                else:
                    summary["stop_reason"] = reason
        except KeyboardInterrupt:
            summary["stop_reason"] = "keyboard_interrupt"
        except Exception:
            # Exceptions may contain request headers: publish fixed reason codes only.
            failed = True
            summary["stop_reason"] = "capture_or_storage_failed"
        finally:
            if adapter is not None:
                adapter.close()
            try:
                summary["status"] = "saving_and_verifying"
                status()
                summary["final_export"] = store.export()
                summary["data_consistency_verified"] = True
                summary["normal_exit"] = not failed
            except Exception:
                failed = True
                summary.update(normal_exit=False, stop_reason="save_or_audit_failed")
            finally:
                store.close()
            summary["status"] = "failed" if failed else "stopped"
            summary["connection_state"] = "stopped"
            summary["finished_at"] = clock().isoformat()
            atomic_write(root / "sessions" / f"{run_id}.json", canonical(summary).encode())
            status()
    return summary
