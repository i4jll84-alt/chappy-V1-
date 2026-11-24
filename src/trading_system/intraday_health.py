"""Local, exchange-session health checks and bounded broker recovery; no Codex dependency."""

import argparse
import gzip
import json
import math
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timedelta
from datetime import time as walltime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_system.brokers.locking import single_writer
from trading_system.storage.store import atomic_write

JST = ZoneInfo("Asia/Tokyo")
PROJECT = Path(__file__).resolve().parents[2]
TASKS = {
    "gui": "JPX-IntradayHealth-Desktop",
    "panel": "JPX-IntradayHealth-Rakuten",
    "kabu_panel": "JPX-IntradayHealth-Kabucom",
}
FOLDERS = {"gui": "desktop-launcher", "panel": "rss-panel", "kabu_panel": "mufg-panel"}
WAITING = {"prompt", "waiting_for_station", "authenticating"}
REOPEN = {"workbook_not_found", "excel_not_registered"}


def now():
    return datetime.now(JST)


def read_json(path, default=None):
    return json.loads(path.read_bytes()) if path.exists() else default


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode(), sharing_retries=20)


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("timezone_required")
    return result.astimezone(JST)


def trading_age(value, moment):
    """Measure quote age in today's trading sessions; lunch is not a data outage."""
    value, moment = stamp(value), moment.astimezone(JST)
    if value.date() != moment.date() or value > moment + timedelta(seconds=5):
        return math.inf
    age = 0.0
    for start, end in [(walltime(9), walltime(11, 30)), (walltime(12, 30), walltime(15, 30))]:
        lo, hi = (datetime.combine(moment.date(), x, JST) for x in (start, end))
        age += max(0, (min(moment, hi) - max(value, lo)).total_seconds())
    return age


def schedule(moment, previous):
    moment = moment.astimezone(JST)
    day = moment.date().isoformat()
    state = previous if previous and previous.get("date") == day else {}
    if moment.weekday() >= 5:
        return {"state": "weekend"}
    if moment.time() < walltime(9, 30):
        return {"state": "before_checks"}
    if moment.time() >= walltime(15, 30):
        return {"state": "session_ended"}
    if walltime(11, 30) <= moment.time() < walltime(12, 30):
        return {"state": "lunch", "next_check": f"{day}T12:30:00+09:00"}
    done = state.get("mandatory_checked", [])
    due = [
        label
        for label in ("09:30", "10:30")
        if label not in done and moment.time() >= walltime.fromisoformat(label)
    ]
    if due:
        return {"state": "due", "reason": "mandatory", "cover_mandatory": due}
    retry = state.get("next_retry")
    if state.get("needs_retry") and retry and moment >= stamp(retry):
        return {"state": "due", "reason": "retry", "cover_mandatory": []}
    return {"state": "finished" if len(done) == 2 else "waiting_1030"}


def next_retry(moment):
    local = moment.astimezone(JST)
    result = local.replace(minute=(local.minute // 30) * 30, second=0, microsecond=0)
    result += timedelta(minutes=30)
    if walltime(11, 30) <= result.time() < walltime(12, 30):
        result = result.replace(hour=12, minute=30)
    return result.isoformat() if result.time() < walltime(15, 30) else None


def calendar_state(project, moment):
    from trading_system.research.daytrade_universe import calendar_days

    days, _ = calendar_days(project / "data/jquants", moment.date(), moment)
    flag = days.get(moment.date().isoformat())
    if flag not in {"0", "1", "2", "3"}:
        raise ValueError("calendar_date_missing")
    return flag in {"1", "2"}


def database_tail(path, broker):
    """Only bounded primary-key tail reads; never aggregate the live tables."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        limit = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > limit), 1000)
        if broker == "panel":
            rows = db.execute(
                "SELECT id,known_at,state,raw_gzip FROM frames ORDER BY id DESC LIMIT 32"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id,known_at,quality,raw_board_gzip,symbol,operation "
                "FROM samples ORDER BY id DESC LIMIT 128"
            ).fetchall()
    return rows


def data_health(project, broker, moment):
    path = project / "data" / FOLDERS[broker] / "research.sqlite"
    if not path.exists():
        return {"latest_id": None, "issues": ["no_saved_observations"]}
    rows = database_tail(path, broker)
    result = {"latest_id": rows[0][0] if rows else None, "issues": []}
    if not rows:
        return {**result, "issues": ["no_saved_observations"]}
    result["latest_saved_at"] = rows[0][1]
    saved_age = (moment - stamp(rows[0][1])).total_seconds()
    if not -5 <= saved_age <= 180:
        result["issues"].append("saved_observations_stale")
    stamps, symbols = [], set()
    for row in rows:
        if (moment - stamp(row[1])).total_seconds() > 300 or not row[3]:
            continue
        payload = json.loads(gzip.decompress(row[3]))
        if broker == "panel":
            if row[2] != "read":
                continue
            from trading_system.research_panel import excel_clock, excel_date

            for item in payload.get("items", []):
                for bar in item.get("bars", []):
                    try:
                        if not all(type(v) in (int, float) and v > 0 for v in bar[2:6]):
                            continue
                        value = datetime.combine(excel_date(bar[0]), excel_clock(bar[1]), JST)
                        if value <= moment and value.date() == moment.date():
                            stamps.append(value.isoformat())
                            symbols.add(item["symbol"])
                    except (ValueError, TypeError, IndexError, OverflowError):
                        continue
        else:
            if row[5] != "board" or row[2] not in {"observed_book", "incomplete_or_crossed_book"}:
                continue
            symbols.add(row[4])
            for key in ("BidTime", "AskTime", "CurrentPriceTime"):
                value = payload.get(key)
                if value:
                    try:
                        if trading_age(value, moment) != math.inf:
                            stamps.append(stamp(value).isoformat())
                    except (ValueError, TypeError):
                        continue
    result["recent_symbols"] = len(symbols)
    result["latest_market_at"] = max(stamps, key=stamp) if stamps else None
    if not stamps or min(trading_age(x, moment) for x in stamps) > 300:
        result["issues"].append("market_data_stale")
    if broker == "kabu_panel" and len(symbols) < 31:
        result["issues"].append("board_cycle_incomplete")
    return result


def snapshot(project, moment=None, *, active=None):
    from trading_system import desktop_launcher as launcher

    moment = (moment or now()).astimezone(JST)
    active = active or (lambda folder: launcher.lock_active(folder / "writer.lock"))
    started = time.monotonic()
    result = {"checked_at": moment.isoformat(), "components": {}, "issues": []}
    for broker, folder in FOLDERS.items():
        root = project / "data" / folder
        item = {"active": False, "issues": [], "manual_stop": False}
        try:
            item["active"] = active(root)
            item["manual_stop"] = any((root / name).exists() for name in ("STOP", "STOP.resuming"))
            if item["manual_stop"]:
                item["issues"].append("manual_stop")
            if not item["active"]:
                item["issues"].append("process_not_running")
            if broker != "gui":
                status = read_json(root / "status.json", {})
                item["state"], item["reason"] = status.get("state"), status.get("reason")
                item["reported_today_symbols"] = status.get(
                    "current_day_chart_symbols"
                    if broker == "panel"
                    else "current_day_board_symbols"
                )
                item["target_symbols"] = status.get("target_symbols", 31)
                if status.get("state") in WAITING:
                    item["issues"].append("operator_authentication_required")
                if status.get("state") in {"failed", "needs_review", "not_started", "stopped"}:
                    item["issues"].append("collector_not_recording")
                observed_at = moment + timedelta(seconds=time.monotonic() - started)
                item["data"] = data_health(project, broker, observed_at)
                item["issues"].extend(item["data"]["issues"])
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            item["issues"].append("inspection_unavailable")
            item["error_type"] = type(exc).__name__
        result["components"][broker] = item
        result["issues"].extend(f"{broker}:{issue}" for issue in item["issues"])
    result["healthy"] = not result["issues"]
    return result


def start_task(broker):
    """Independent scheduler workers survive the short health-check task ending."""
    if broker not in TASKS:
        raise ValueError("unknown_health_worker")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run(
        [
            str(shell),
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            f"Start-ScheduledTask -TaskPath '\\' -TaskName '{TASKS[broker]}' -ErrorAction Stop",
        ],
        check=True,
        capture_output=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def recovery_actions(before, starter):
    actions = []
    for broker, item in before["components"].items():
        if item["manual_stop"] or "inspection_unavailable" in item["issues"]:
            continue
        recoverable = (
            not item["active"]
            or (broker == "panel" and item.get("reason") in REOPEN)
            or item.get("state") == "waiting_for_station"
            or (
                broker != "gui"
                and item.get("state") not in WAITING
                and any(
                    x in item["issues"] for x in ("saved_observations_stale", "market_data_stale")
                )
            )
        )
        if not recoverable:
            continue
        try:
            starter(broker)
            actions.append({"component": broker, "state": "worker_requested"})
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            actions.append(
                {
                    "component": broker,
                    "state": "recovery_failed",
                    "error_type": type(exc).__name__,
                }
            )
    return actions


def run(
    project,
    *,
    clock=now,
    sleeper=time.sleep,
    probe=snapshot,
    starter=start_task,
    session_check=calendar_state,
):
    project = Path(project).resolve()
    root = project / "data/intraday-health"
    moment = clock().astimezone(JST)
    if (root / "STOP").exists():
        return {"state": "disabled"}
    with single_writer(root):
        try:
            previous = read_json(root / "state.json", {})
            if not isinstance(previous, dict) or not isinstance(
                previous.get("mandatory_checked", []), list
            ):
                raise ValueError("invalid_health_state")
        except (ValueError, TypeError):
            previous = {}
        decision = schedule(moment, previous)
        if decision["state"] != "due":
            return decision
        report = {"started_at": moment.isoformat(), "schedule": decision, "actions": []}
        day = moment.date().isoformat()
        prior = previous if previous.get("date") == day else {}
        try:
            if not session_check(project, moment):
                return {"state": "market_closed", "date": day}
            before = probe(project, moment)
            report["before"] = before
            report["actions"] = recovery_actions(before, starter)
            sleeper(75 if report["actions"] else 20)
            after = probe(project, clock())
            # A recent status alone cannot prove that the collector is advancing.
            for broker in ("panel", "kabu_panel"):
                old = before["components"][broker].get("data", {}).get("latest_id")
                new = after["components"][broker].get("data", {}).get("latest_id")
                if old is not None and new is not None and new <= old:
                    after["issues"].append(f"{broker}:no_progress_during_verification")
            after["healthy"] = not after["issues"]
            report["after"] = after
            healthy = after["healthy"]
            report["state"] = "healthy" if healthy else "needs_attention"
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            healthy = False
            report.update(state="needs_attention", error_type=type(exc).__name__)
        done = sorted(set(prior.get("mandatory_checked", []) + decision["cover_mandatory"]))
        state = {
            "date": day,
            "mandatory_checked": done,
            "needs_retry": not healthy,
            "next_retry": next_retry(clock()) if not healthy else None,
            "next_mandatory": None if "10:30" in done else f"{day}T10:30:00+09:00",
            "last_result": report["state"],
            "checked_at": clock().isoformat(),
        }
        report["finished_at"], report["next"] = clock().isoformat(), state
        filename = moment.strftime("%H%M%S-%f") + ".json"
        save(root / "runs" / day / filename, report)
        save(root / "status.json", report)
        save(root / "state.json", state)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument(
        "--inspect", action="store_true", help="Read health only; no recovery or schedule changes"
    )
    args = parser.parse_args(argv)
    try:
        result = snapshot(args.project) if args.inspect else run(args.project)
    except ValueError as exc:
        if str(exc) != "broker_state_already_open":
            raise
        result = {"state": "already_running"}
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result.get("state") == "needs_attention" else 0


if __name__ == "__main__":
    raise SystemExit(main())
