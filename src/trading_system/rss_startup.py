"""Classify saved startup probes without relaxing RSS data validation."""

from datetime import date, datetime
from pathlib import Path

from trading_system.point_in_time.policy import aware_utc
from trading_system.runtime.models import JST
from trading_system.storage.rss import inspect_record


def assess_startup(folder: Path, code: str, now: datetime) -> dict:
    """Only a complete batch of three valid current-day reads releases capture."""
    now = aware_utc(now)
    paths = sorted(folder.glob("[0-9][0-9][0-9].json"))
    if [p.name for p in paths] != ["000.json", "001.json", "002.json"]:
        return {"state": "blocked", "issues": ["startup_sample_count"]}
    issues = set()
    waiting = False
    blocked = False
    for path in paths:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("observation_too_large")
        checked = inspect_record(path.read_bytes(), now)
        issues.update(checked["issues"])
        if checked.get("code") != code:
            issues.add("unexpected_code")
            blocked = True
        elif checked["status"] == "observed":
            # Recheck against the startup clock, not only the record's own date.
            if checked["normalized"]["current_date"] != now.astimezone(JST).date().isoformat():
                issues.add("startup_date_changed")
                blocked = True
        elif checked["issues"] == ["current_date_not_today"]:
            displayed = date.fromisoformat(checked["normalized"]["current_date"])
            if displayed < now.astimezone(JST).date():
                waiting = True
            else:
                issues.add("future_current_date")
                blocked = True
        else:
            blocked = True
    state = "blocked" if blocked else "waiting_for_current_date" if waiting else "ready"
    return {"state": state, "issues": sorted(issues)}
