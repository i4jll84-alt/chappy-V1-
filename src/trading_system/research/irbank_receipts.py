"""Inspect every dated IRBANK execution receipt locally, without launching acquisition."""

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
RUN_FILE = re.compile(r"([0-9]{6}-[0-9]{6})(\.stdout\.log|\.json)")
OUTCOMES = {
    "archive_complete",
    "daily_quota_reached",
    "trial_budget_exhausted",
    "expired",
    "day_changed",
    "needs_attention",
}
BRANCH_STATES = {"complete", "waiting", "needs_attention"}
PHASES = {
    "first5_required",
    "history_required",
    "first5",
    "history",
    "masters",
    "weekly",
    "financials",
    "shareholders",
}
REASONS = {
    "remote_daily_headroom",
    "authorized_daily_data_limit",
    "trial_total_request_limit",
    "outside_free_trial_window",
    "resume_on_next_scheduled_run",
    "day_changed_resume",
    "daily_batch_limit",
    "retry_exhausted",
    "overlapping_pages_conflict",
}


def timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("missing timezone")
    return stamp.astimezone(JST)


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid count")
    return value


def quota_counts(values, used, limit, remaining, *, remote=False):
    counts = [
        count(values[k]) if values.get(k) is not None else None for k in (used, limit, remaining)
    ]
    used, limit, remaining = counts
    if limit is not None and (
        (remaining is not None and remaining > limit)
        or (remote and used is not None and used > limit)
        or (None not in counts and remaining != max(0, limit - used))
    ):
        raise ValueError("inconsistent quota counts")
    return {"used": used, "limit": limit, "remaining": remaining}


def quotas(receipt, after_day, end):
    after = receipt["after"]
    daily = quota_counts(after, "daily_data_attempts", "daily_data_limit", "remaining_daily_data")
    trial = quota_counts(after, "trial_data_attempts", "total_data_limit", "remaining_trial_data")
    authorized = quota_counts(
        after, "authorized_period_data_attempts", "total_data_limit", "remaining_authorized_data"
    )
    observed = timestamp(after["checked_at"]) if after.get("checked_at") is not None else None
    if observed is not None and (observed.date() != after_day or observed > end):
        raise ValueError("inconsistent local quota date")
    daily.update(
        date=after_day.isoformat(),
        trial_remaining=trial["remaining"],
        authorized_remaining=authorized["remaining"],
        checked_at=observed.isoformat() if observed is not None else None,
    )
    saved = receipt.get("last_remote_usage")
    if saved is None:
        return daily, None
    remote = quota_counts(saved, "used", "limit", "remaining", remote=True)
    checked = timestamp(saved["checked_at"]) if saved.get("checked_at") is not None else None
    if checked is not None and checked > end:
        raise ValueError("future provider quota")
    remote["checked_at"] = checked.isoformat() if checked is not None else None
    remote["same_local_day"] = checked.date() == after_day if checked is not None else None
    return daily, remote


def describe(receipt, day):
    outcome = receipt["state"]
    start = timestamp(receipt["started_at"])
    end = timestamp(receipt["finished_at"])
    after_day = date.fromisoformat(receipt["after"]["date"])
    if (
        outcome not in OUTCOMES
        or start.date() != day
        or receipt["before"]["date"] != day.isoformat()
        or not day <= after_day <= end.date()
        or end < start
    ):
        raise ValueError("inconsistent execution")
    data_requests = count(receipt["data_attempts_this_execution"])
    http_attempts = count(receipt["http_attempts_this_execution"])
    batches = receipt["batches"]
    if not isinstance(batches, list):
        raise ValueError("invalid batches")
    if http_attempts != sum(count(b["requests_this_run"]) for b in batches):
        raise ValueError("inconsistent HTTP count")
    # Per-batch counts: successive batches can inspect the same jobs.
    empty = [count(b["empty"]) for b in batches]
    errors = [count(b["errors"]) for b in batches]
    remaining = [count(b["remaining"]) for b in batches]
    phases = [b.get("phase") for b in batches]
    if any(phase is not None and phase not in PHASES for phase in phases):
        raise ValueError("unrecognized acquisition phase")
    if any(b["state"] not in {"complete", "stopped", "failed"} for b in batches) or (
        outcome == "archive_complete"
        and (not batches or batches[-1]["state"] != "complete" or remaining[-1] != 0)
    ):
        raise ValueError("inconsistent batches")
    comparison = receipt["comparison"]
    state = comparison.get("state")
    branches = {
        key: comparison.get("branches", {}).get(key, {}).get("state")
        for key in ("original", "approx")
    }
    if state in {"complete", "waiting"}:
        valid = all(v in {"complete", "waiting"} for v in branches.values()) and (
            state == "complete"
        ) == all(v == "complete" for v in branches.values())
    else:
        valid = state in {"disabled", "already_running", "needs_attention"}
    if not valid or any(v is not None and v not in BRANCH_STATES for v in branches.values()):
        raise ValueError("inconsistent comparison")
    status = "finished"
    if (
        outcome == "needs_attention"
        or state == "needs_attention"
        or any(errors)
        or any(b["state"] == "failed" for b in batches)
    ):
        status = "error"
    elif outcome == "day_changed" or state in {"already_running", "disabled"}:
        status = "partial"
    reason = receipt.get("reason")
    local, remote = quotas(receipt, after_day, end)
    return {
        "status": status,
        "outcome": outcome,
        "started_at": start.isoformat(),
        "finished_at": end.isoformat(),
        "data_requests": data_requests,
        "http_attempts": http_attempts,
        "reason": reason if reason is None or reason in REASONS else "see_receipt",
        "local_quota": local,
        "provider_quota": remote,
        "batch_phases": phases,
        "batch_empty": empty,
        "batch_errors": errors,
        "batch_remaining": remaining,
        "comparison": state,
        "branches": branches,
    }


def inspect_day(root, day):
    folder = root / day.isoformat()
    files = {}
    try:
        for path in folder.iterdir():
            match = RUN_FILE.fullmatch(path.name)
            if match and path.is_file():
                files.setdefault(match[1], {})[match[2]] = path
    except FileNotFoundError:
        pass
    except OSError:
        return [{"status": "unverified", "reason": "directory_unreadable"}]
    if not files:
        return [{"status": "unverified", "reason": "no_timestamped_runs"}]
    rows = []
    for run, paths in sorted(files.items()):
        row = {"run": run, "status": "unverified"}
        if ".json" not in paths:
            row["reason"] = "no_finished_receipt"
        else:
            try:
                row.update(describe(json.loads(paths[".json"].read_bytes()), day))
                if ".stdout.log" not in paths and row["batch_errors"]:
                    row.update(status="unverified", reason="missing_stdout")
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                row["reason"] = "unreadable_or_inconsistent_receipt"
        rows.append(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--through", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    if args.from_date > args.through:
        parser.error("--through must be on or after --from-date")
    root = args.project / "data/irbank_jsf/daily"
    attention = False
    for ordinal in range(args.from_date.toordinal(), args.through.toordinal() + 1):
        day = date.fromordinal(ordinal)
        for row in inspect_day(root, day):
            print(json.dumps({"day": day.isoformat(), **row}, separators=(",", ":")))
            attention |= row["status"] != "finished"
    return 2 if attention else 0


if __name__ == "__main__":
    raise SystemExit(main())
