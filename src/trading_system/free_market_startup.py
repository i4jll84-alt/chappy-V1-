"""One catch-up attempt 30 minutes after the Codex desktop process starts."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from trading_system.brokers.locking import single_writer
from trading_system.free_market import schedule_decision
from trading_system.storage.store import atomic_write, canonical


def check_startup(root, started, pid, *, moment=None, runner=subprocess.run):
    moment = moment or datetime.now(UTC)
    if started.tzinfo is None or moment.tzinfo is None or pid <= 0:
        raise ValueError("invalid_app_start")
    if started > moment:
        raise ValueError("future_app_start")
    if moment < started + timedelta(minutes=30):
        return {"state": "waiting_for_30_minutes"}, 0
    root = root.resolve()
    scheduler = root / "scheduler"
    scheduler.mkdir(parents=True, exist_ok=True)
    key = f"{pid}:{started.astimezone(UTC).isoformat()}"
    with single_writer(scheduler):
        status_path = scheduler / "startup-status.json"
        previous = json.loads(status_path.read_text("utf-8")) if status_path.exists() else {}
        if previous.get("app_instance") == key and previous.get("state") in (
            "checked",
            "already_acquired",
            "stopped",
            "failed",
        ):
            return {"state": "startup_already_handled"}, 0
        result = {
            "app_instance": key,
            "app_started_at": started.isoformat(),
            "checked_at": moment.isoformat(),
            **schedule_decision(root, moment),
        }

        def save():
            atomic_write(status_path, canonical(result).encode())

        if result["state"] != "due":
            result["exit_code"] = 0
            save()
            return result, 0
        log = scheduler / (moment.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8] + ".log")
        result.update(state="running", log=str(log.relative_to(root)))
        save()
        try:
            with log.open("w", encoding="utf-8") as output:
                completed = runner(
                    [
                        sys.executable,
                        "-m",
                        "trading_system.free_market",
                        "--root",
                        str(root),
                        "--due",
                    ],
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            lines = log.read_text("utf-8").splitlines()
            report = json.loads(lines[-1]) if lines else {}
            result["exit_code"] = completed.returncode
            result["result"] = report
            valid = report.get("state") in (
                "checked",
                "already_acquired",
                "stopped",
                "already_running",
            )
            if completed.returncode == 0 and valid and not report.get("issues"):
                result["state"] = report["state"]
            else:
                result["state"] = "failed"
                result["exit_code"] = completed.returncode or 1
        except (OSError, ValueError) as exc:
            # Preserve the raw log; never record an exception as acquisition success.
            result.update(state="failed", exit_code=1, error=type(exc).__name__)
        result["finished_at"] = datetime.now(UTC).isoformat()
        save()
        return result, result["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/free-market/public"))
    parser.add_argument("--app-started-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--app-pid", type=int, required=True)
    args = parser.parse_args()
    try:
        result, code = check_startup(args.root, args.app_started_at, args.app_pid)
    except ValueError as exc:
        if str(exc) != "broker_state_already_open":
            raise
        result, code = {"state": "startup_check_already_running"}, 0
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
