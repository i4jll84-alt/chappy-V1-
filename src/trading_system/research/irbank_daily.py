"""Daily, trial-bounded IRBANK archival, prioritizing Plan A's first five years."""

import argparse
import json
import sqlite3
from contextlib import closing, redirect_stdout
from datetime import date, datetime
from pathlib import Path

from trading_system.brokers.locking import single_writer

from . import irbank_jsf as jsf


def inspect(root, stamp=None):
    root = Path(root)
    stamp = (stamp or jsf.now()).astimezone(jsf.JST)
    policy = json.loads((root / "acquisition-policy.json").read_bytes())
    if policy.get("quota_accounting") != "data_only":
        raise jsf.Halt("daily_requires_data_only_accounting")
    schedule = policy["daily_schedule"]
    start = date.fromisoformat(schedule["from"])
    through = date.fromisoformat(schedule["through"])
    begins = datetime.fromisoformat(policy["starts_at"])
    ends = datetime.fromisoformat(policy["ends_before"])
    if (
        begins.utcoffset() is None
        or ends.utcoffset() is None
        or not begins.date() <= start <= through <= ends.date()
        or type(schedule["max_data_requests"]) is not int
        or not 1 <= schedule["max_data_requests"] <= 1000
        or type(policy["max_total_requests"]) is not int
        or not 1 <= policy["max_total_requests"] <= 10000
        or type(policy.get("initial_data_requests")) is not int
        or not 0 <= policy["initial_data_requests"] <= policy["initial_requests"]
    ):
        raise jsf.Halt("invalid_daily_policy")
    day = stamp.date().isoformat()
    # Read-only: a missing request ledger must never reset the allowance.
    with closing(
        sqlite3.connect((root / "requests.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        daily = db.execute(
            "SELECT count(*) FROM requests WHERE day=? AND kind='data'", (day,)
        ).fetchone()[0]
        total = db.execute("SELECT count(*) FROM requests WHERE kind='data'").fetchone()[0]
    daily += policy["initial_data_requests"] if stamp.date() == begins.date() else 0
    total += policy["initial_data_requests"]
    state = "due"
    if stamp >= ends or stamp.date() > through:
        state = "expired"
    elif stamp < begins or stamp.date() < start:
        state = "not_started"
    elif total >= policy["max_total_requests"]:
        state = "trial_budget_exhausted"
    elif daily >= schedule["max_data_requests"]:
        state = "daily_quota_reached"
    return {
        "state": state,
        "date": day,
        "checked_at": stamp.isoformat(),
        "first_date": start.isoformat(),
        "last_date": through.isoformat(),
        "ends_before": ends.isoformat(),
        "daily_data_attempts": daily,
        "total_data_attempts": total,
        "daily_data_limit": schedule["max_data_requests"],
        "total_data_limit": policy["max_total_requests"],
        "remaining_daily_data": max(0, schedule["max_data_requests"] - daily),
        "remaining_trial_data": max(0, policy["max_total_requests"] - total),
    }


def run(project):
    project = Path(project).resolve()
    root = project / "data/irbank_jsf"
    first = inspect(root)
    if first["state"] != "due":
        return first
    with single_writer(root / "daily-dispatch"):
        first = inspect(root)
        if first["state"] != "due":
            return first
        day = first["date"]
        folder = root / "daily" / day
        folder.mkdir(parents=True, exist_ok=True)
        report = {
            "state": "running",
            "started_at": jsf.now().isoformat(),
            "before": first,
            "batches": [],
        }
        stamp = jsf.now().strftime("%H%M%S-%f")
        phase = "first5"
        for _ in range(6):
            current = inspect(root)
            if current["state"] != "due":
                report["state"] = current["state"]
                break
            if current["date"] != day:
                report.update(state="day_changed", reason="resume_on_next_scheduled_run")
                break
            # Retain the original per-process ceiling of 950 HTTP attempts.
            with (folder / (stamp + ".stdout.log")).open("a", encoding="utf-8") as log:
                with redirect_stdout(log):
                    batch = jsf.run(project, root, 950, phase)
            report["batches"].append({"phase": phase, **batch})
            reason = batch.get("reason")
            if batch["state"] == "complete":
                if phase == "first5":
                    phase = "history"
                    continue
                report["state"] = "archive_complete"
                break
            if reason == "run_request_limit" and batch["requests_this_run"] > 0:
                continue
            if reason in ("remote_daily_headroom", "authorized_daily_data_limit"):
                report["state"] = "daily_quota_reached"
            elif reason == "trial_total_request_limit":
                report["state"] = "trial_budget_exhausted"
            elif reason == "outside_free_trial_window":
                report["state"] = "expired"
            elif reason == "day_changed_resume":
                report["state"] = "day_changed"
            else:
                report["state"] = "needs_attention"
            report["reason"] = reason
            break
        else:
            report.update(state="needs_attention", reason="daily_batch_limit")
        report["after"] = inspect(root)
        report["finished_at"] = jsf.now().isoformat()
        report["data_attempts_this_execution"] = (
            report["after"]["total_data_attempts"] - first["total_data_attempts"]
        )
        report["http_attempts_this_execution"] = sum(
            b["requests_this_run"] for b in report["batches"]
        )
        usage = root / "last-usage.json"
        if usage.exists():
            report["last_remote_usage"] = json.loads(usage.read_bytes())
        jsf.save(folder / (stamp + ".json"), report)
        jsf.save(folder / "latest.json", report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--inspect", action="store_true", help="Read local readiness; no API calls")
    args = parser.parse_args(argv)
    try:
        result = inspect(args.project / "data/irbank_jsf") if args.inspect else run(args.project)
    except (jsf.Halt, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        reason = str(exc) if isinstance(exc, jsf.Halt) else type(exc).__name__
        result = {"state": "needs_attention", "reason": reason}
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result["state"] == "needs_attention" else 0


if __name__ == "__main__":
    raise SystemExit(main())
