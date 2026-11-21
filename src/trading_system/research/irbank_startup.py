"""Resume a missed IRBANK evening run while the Codex desktop app is available."""

import argparse
import json
import os
import sqlite3
import subprocess
import tomllib
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from trading_system.brokers.locking import single_writer

from . import irbank_daily as daily
from . import irbank_jsf as jsf


def detect_app():
    """Use the OS's read-only process query; no optional Python dependency or Codex version path."""
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    query = r"""
$session = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
$app = Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe' OR Name='Codex.exe'" |
 Where-Object {
  $_.SessionId -eq $session -and
  $_.ExecutablePath -match '\\OpenAI\.Codex_[^\\]+\\app\\(ChatGPT|Codex)\.exe$' -and
  $_.CommandLine -notmatch '(?i)(--type(?:=|\s)|\s(?:app-server|exec-server)(?:\s|$))'
 } | Sort-Object CreationDate | Select-Object -First 1
if ($app) {
 [pscustomobject]@{pid=$app.ProcessId;started=$app.CreationDate.ToUniversalTime().ToString('o')} |
 ConvertTo-Json -Compress
}
"""
    response = subprocess.run(
        [
            str(shell),
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            "$ErrorActionPreference='Stop';" + query,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        check=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not response.stdout.strip():
        return None
    result = json.loads(response.stdout)
    return datetime.fromisoformat(result["started"]), int(result["pid"])


def assess(project, automation, moment=None):
    project = Path(project).resolve()
    root = project / "data/irbank_jsf"
    moment = (moment or jsf.now()).astimezone(jsf.JST)
    if not Path(automation).exists():
        return {"state": "automation_disabled"}
    config = tomllib.loads(Path(automation).read_text(encoding="utf-8"))
    if config.get("id") != "irbank-5" or config.get("status") != "ACTIVE":
        return {"state": "automation_disabled"}
    current = daily.inspect(root, moment)
    if current["state"] != "due":
        return current
    policy = json.loads((root / "acquisition-policy.json").read_bytes())
    at = time.fromisoformat(policy["daily_schedule"].get("time_jst", "21:00"))
    if at.tzinfo is not None:
        raise ValueError("schedule_time_must_be_local")
    due = datetime.combine(moment.date(), at, jsf.JST)
    if moment < due:
        due -= timedelta(days=1)
    first = datetime.combine(datetime.fromisoformat(current["first_date"]).date(), at, jsf.JST)
    if due < first:
        return {**current, "state": "waiting_for_first_schedule"}
    due_day = due.date().isoformat()
    report = {**current, "due_at": due.isoformat()}
    previous_day = daily.inspect(root, due)
    if previous_day["daily_data_attempts"] >= current["daily_data_limit"]:
        return {**report, "state": "scheduled_quota_already_met"}
    receipt = root / "daily" / due_day / "latest.json"
    if receipt.exists():
        saved = json.loads(receipt.read_bytes())
        if saved.get("state") == "archive_complete":
            return {**report, "state": "archive_complete"}
        quota = saved.get("last_remote_usage", {})
        checked = datetime.fromisoformat(quota["checked_at"]) if quota.get("checked_at") else None
        if (
            saved.get("state") == "daily_quota_reached"
            and saved.get("after", {}).get("date") == due_day
            and checked is not None
            and checked.tzinfo is not None
            and checked.astimezone(jsf.JST).date().isoformat() == due_day
            and quota.get("remaining") == 0
            and quota.get("used") == quota.get("limit")
        ):
            return {**report, "state": "scheduled_quota_already_met"}
    return report


def check(project, automation, started, pid, *, moment=None, runner=None):
    moment = moment or jsf.now()
    if started.tzinfo is None or moment.tzinfo is None or started > moment or pid <= 0:
        raise ValueError("invalid_app_start")
    project = Path(project).resolve()
    root = project / "data/irbank_jsf"
    decision = assess(project, automation, moment)
    if decision["state"] != "due":
        return decision
    folder = root / "startup"
    with single_writer(folder):
        decision = assess(project, automation, moment)
        if decision["state"] != "due":
            return decision
        key = f"{pid}:{started.astimezone(UTC).isoformat()}:{decision['due_at']}"
        status = folder / "status.json"
        previous = json.loads(status.read_bytes()) if status.exists() else {}
        if previous.get("attempt_key") == key and previous.get("state") in ("finished", "failed"):
            return {"state": "startup_already_handled", "due_at": decision["due_at"]}
        result = {
            "state": "running",
            "attempt_key": key,
            "app_pid": pid,
            "app_started_at": started.isoformat(),
            "started_at": moment.isoformat(),
            "due_at": decision["due_at"],
            "before": decision,
        }
        jsf.save(status, result)
        try:
            outcome = (runner or daily.run)(project)
            if outcome.get("state") in (
                "daily_quota_reached",
                "trial_budget_exhausted",
                "expired",
                "archive_complete",
            ):
                result["state"] = "finished"
            elif outcome.get("state") == "day_changed":
                result["state"] = "interrupted"
            else:
                result["state"] = "failed"
            result["result"] = outcome
        except ValueError as exc:
            if str(exc) == "broker_state_already_open":
                result.update(state="already_running", reason="daily_dispatch_locked")
            else:
                result.update(state="failed", reason=type(exc).__name__)
        except (jsf.Halt, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            result.update(state="failed", reason=type(exc).__name__)
        result["finished_at"] = jsf.now().isoformat()
        jsf.save(status, result)
        filename = moment.astimezone(jsf.JST).strftime("%Y%m%dT%H%M%S-%f") + ".json"
        jsf.save(folder / filename, result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--automation", type=Path, required=True)
    parser.add_argument("--app-started-at", type=datetime.fromisoformat)
    parser.add_argument("--app-pid", type=int)
    parser.add_argument("--detect-app", action="store_true")
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.detect_app:
            app = detect_app()
            if app is None:
                print(json.dumps({"state": "codex_not_running"}))
                return 0
            args.app_started_at, args.app_pid = app
        elif args.app_started_at is None or args.app_pid is None:
            parser.error("provide --detect-app or both app start time and pid")
        if args.inspect:
            result = assess(args.project, args.automation)
        else:
            result = check(args.project, args.automation, args.app_started_at, args.app_pid)
    except ValueError as exc:
        result = (
            {"state": "already_running"}
            if str(exc) == "broker_state_already_open"
            else {"state": "failed", "reason": type(exc).__name__}
        )
    except (
        jsf.Halt,
        KeyError,
        TypeError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        result = {"state": "failed", "reason": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result["state"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
