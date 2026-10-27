"""Continuous read-only RSS capture and optional local paper accounting smoke test."""

import argparse
import json
import math
import os
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from trading_system.data_sources.rss_live import observe
from trading_system.monitoring.rss import audit, audit_runs
from trading_system.paper_rss import PaperLedger
from trading_system.point_in_time.policy import aware_utc
from trading_system.runtime.models import daytime_deadline
from trading_system.storage.rss import RSSStore
from trading_system.storage.store import atomic_write, canonical


@contextmanager
def single_writer(root):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "collector.lock").open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("collector_already_running") from exc
        yield
    finally:
        handle.close()  # OS releases the lock, also on process exit.


def run(
    *,
    workbook,
    code,
    root,
    cycles=0,
    interval=2,
    paper=False,
    export_every=30,
    observer=observe,
    sleeper=time.sleep,
    utcnow=lambda: datetime.now(UTC),
    recover_only=False,
    progress=None,
):
    if not re.fullmatch(r"[0-9A-Z]{4,5}\.T", code):
        raise ValueError("invalid_code")
    if cycles < 0 or not math.isfinite(interval) or not 1 <= interval <= 30 or export_every < 1:
        raise ValueError("invalid_limits")
    root = root.resolve()
    started = aware_utc(utcnow())
    deadline = daytime_deadline(started)
    postprocessing = recover_only or started >= deadline
    with single_writer(root):
        config = dict(workbook=str(workbook.resolve()), code=code, paper=paper, interval=interval)
        config_path = root / "config.json"
        if config_path.exists() and json.loads(config_path.read_bytes()) != config:
            raise ValueError("collector_configuration_changed_use_new_root")
        atomic_write(config_path, canonical(config).encode())
        if (root / "STOP").exists() and not postprocessing:
            raise ValueError("stop_requested_remove_STOP_to_start")
        session_id = uuid4().hex
        report = dict(
            schema_version=1,
            session_id=session_id,
            pid=os.getpid(),
            started_at=started.isoformat(),
            status="running",
            purpose="postprocessing" if postprocessing else "daytime",
            phase="restoring",
            normal_exit=False,
            scheduled_stop_at=deadline.isoformat(),
            stop_delayed=False,
            delay_reason=None,
            real_execution_enabled=False,
            freshness_verified=False,
            cycles_completed=0,
            samples=0,
            excel_reads=0,
            attention_samples=0,
            recovered_samples=0,
            recovered_incomplete_runs=0,
        )
        # Keep old session files unchanged, including evidence of interrupted shutdown.
        report["previous_unfinished_sessions"] = [
            p.stem
            for p in sorted((root / "sessions").glob("*.json"))
            if json.loads(p.read_bytes()).get("status") in ("running", "stopping", "failed")
        ]
        ledger = None
        runs_root = root / "runs"
        runs_root.mkdir(exist_ok=True)

        def refresh_paper():
            if ledger:
                report["paper"] = ledger.snapshot()
                if not report["paper"]["accounting_matches"]:
                    raise ValueError("paper_accounting_mismatch")

        def persist():
            now = aware_utc(utcnow())
            report["updated_at"] = now.isoformat()
            report["stop_delayed"] = bool(
                not postprocessing and not report["normal_exit"] and now >= deadline
            )
            report["delay_reason"] = report["phase"] if report["stop_delayed"] else None
            atomic_write(root / "sessions" / f"{session_id}.json", canonical(report).encode())
            atomic_write(root / "status.json", canonical(report).encode())

        def stop_requested():
            reason = None
            if (root / "STOP").exists():
                reason = "stop_file"
            elif aware_utc(utcnow()) >= deadline:
                reason = "daytime_1600"
            if reason:
                if "stop_reason" not in report:
                    report.update(
                        stop_reason=reason,
                        stop_requested_at=aware_utc(utcnow()).isoformat(),
                        status="stopping",
                        purpose="preparing_stop",
                        phase="finishing_current_read",
                    )
                    persist()
                return True
            return False

        def ingest(store, folder, recovery=False):
            result = store.import_run(folder)
            if ledger:
                for path in sorted(folder.glob("[0-9][0-9][0-9].json")):
                    ledger.consume(
                        f"{folder.name}/{path.name}",
                        path.read_bytes(),
                        aware_utc(utcnow()),
                        recovery=recovery,
                    )
            return result

        def recover(store):
            folders = sorted(
                (p for p in runs_root.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime_ns,
            )
            incomplete = 0
            for folder in folders:
                if re.fullmatch(r"[0-9a-f]{32}", folder.name) and any(
                    folder.glob("[0-9][0-9][0-9].json")
                ):
                    result = ingest(store, folder, recovery=True)
                    report["recovered_samples"] += result["imported"]
                    incomplete += int(not (folder / "summary.json").exists())
            report["recovered_incomplete_runs"] = incomplete

        def phase(value):
            report.update(status="stopping", phase=value)
            if not postprocessing:
                report["purpose"] = "preparing_stop"
            refresh_paper()
            persist()
            if progress:
                progress(
                    {
                        "status": "stopping",
                        "phase": value,
                        "stop_reason": report.get("stop_reason"),
                        "scheduled_stop_at": report["scheduled_stop_at"],
                        "real_execution_enabled": False,
                    }
                )

        try:
            ledger = PaperLedger(root / "paper.sqlite", code) if paper else None
            refresh_paper()
            persist()
            with RSSStore(root / "store") as store:
                recover(store)
                refresh_paper()
                report["phase"] = "capturing" if not postprocessing else "recovering"
                persist()
                if postprocessing:
                    report["stop_reason"] = "postprocessing_complete"
                else:
                    try:
                        while not stop_requested():
                            folder, summary = observer(
                                workbook,
                                code,
                                runs_root,
                                samples=5,
                                interval=interval,
                                timeout=10,
                                stop_requested=stop_requested,
                            )
                            if folder is not None:
                                ingest(store, folder)
                                report["last_run_id"] = folder.name
                                report["last_observation"] = summary
                                report["cycles_completed"] += 1
                                report["samples"] += summary["samples"]
                                report["excel_reads"] += summary["excel_reads"]
                                report["attention_samples"] += summary["attention_samples"]
                                refresh_paper()
                                persist()
                            if stop_requested():
                                break
                            if folder is None:
                                raise ValueError("observer_returned_no_samples")
                            if report["cycles_completed"] % export_every == 0:
                                report["export"] = store.export()
                            if cycles and report["cycles_completed"] >= cycles:
                                report["stop_reason"] = "verification_cycle_limit"
                                break
                            end = time.monotonic() + interval
                            while time.monotonic() < end and not stop_requested():
                                sleeper(min(0.2, max(0, end - time.monotonic())))
                    except KeyboardInterrupt:
                        report["stop_reason"] = "keyboard_interrupt"
                        recover(store)
                phase("saving_parquet")
                report["export"] = store.export()
            phase("verifying_saved_data")
            report["audit"] = audit(root / "store")
            report["coverage"] = audit_runs(root / "store", runs_root)
            # Historical read errors and absent old summaries remain visible warnings.
            audit_errors = set(report["audit"]["issues"]) - {"observation_attention", "no_imports"}
            coverage_errors = set(report["coverage"]["issues"]) - {
                "run_summary_missing",
                "source_observation_attention",
                "no_source_runs",
            }
            if audit_errors or coverage_errors:
                raise ValueError("rss_saved_data_inconsistent")
            refresh_paper()
            finished = aware_utc(utcnow())
            report.update(
                status="stopped",
                phase="complete",
                normal_exit=True,
                finished_at=finished.isoformat(),
                stop_delay_seconds=max(0, (finished - deadline).total_seconds())
                if not postprocessing
                else 0,
                data_consistency_verified=True,
            )
            persist()
        except Exception as exc:
            report.update(
                status="failed",
                phase="needs_review",
                normal_exit=False,
                error_type=type(exc).__name__,
                finished_at=aware_utc(utcnow()).isoformat(),
            )
            # A failed final status write must not leave a successful session record.
            try:
                persist()
            except Exception:
                pass  # The original failure reaches the CLI; never print a success message.
            raise
        finally:
            if ledger:
                ledger.close()
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--code", default="7203.T")
    parser.add_argument("--root", type=Path, default=Path("data/rss-continuous"))
    parser.add_argument("--cycles", type=int, default=0, help="0: until STOP file or Ctrl+C")
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument(
        "--paper", action="store_true", help="One fixed local virtual roundtrip only"
    )
    parser.add_argument(
        "--recover-only", action="store_true", help="Offline recovery/export; no Excel reads"
    )
    args = parser.parse_args(argv)
    try:
        result = run(**vars(args), progress=lambda value: print(json.dumps(value), flush=True))
    except Exception as exc:
        print(
            json.dumps(
                dict(
                    status="failed",
                    normal_exit=False,
                    error_type=type(exc).__name__,
                    reason="check_status_or_existing_collector",
                )
            )
        )
        return 2
    print(
        json.dumps(
            dict(
                status=result["status"],
                normal_exit=result["normal_exit"],
                purpose=result["purpose"],
                stop_reason=result["stop_reason"],
                recovered_samples=result["recovered_samples"],
                stop_delay_seconds=result["stop_delay_seconds"],
                root=str(args.root),
                cycles=result["cycles_completed"],
                samples=result["samples"],
                attention=result["attention_samples"],
                paper=result.get("paper"),
            ),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
