"""Classify saved startup probes without relaxing RSS data validation."""

import json
import re
from datetime import date, datetime
from pathlib import Path

from trading_system.point_in_time.policy import aware_utc
from trading_system.runtime.models import JST
from trading_system.storage.rss import inspect_record

WAITING_STATES = frozenset({"waiting_for_current_date", "waiting_for_quote"})


def _empty_quote(body: bytes) -> bool:
    """Match the observed empty quote only after inspect_record has validated the read."""
    price, clock, _ = json.loads(body)["raw"]["values_after"]
    return (
        not isinstance(price, bool)
        and isinstance(price, (int, float))
        and price == 0
        and isinstance(clock, str)
        and re.fullmatch(r" *: *: *", clock) is not None
    )


def assess_startup(folder: Path, code: str, now: datetime) -> dict:
    """Only a complete batch of three valid current-day reads releases capture."""
    now = aware_utc(now)
    paths = sorted(folder.glob("[0-9][0-9][0-9].json"))
    if [p.name for p in paths] != ["000.json", "001.json", "002.json"]:
        return {"state": "blocked", "issues": ["startup_sample_count"]}
    issues = set()
    waiting_date = False
    waiting_quote = False
    blocked = False
    for path in paths:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("observation_too_large")
        body = path.read_bytes()
        checked = inspect_record(body, now)
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
                waiting_date = True
            else:
                issues.add("future_current_date")
                blocked = True
        elif checked["issues"] == ["invalid_price", "invalid_quote_time"] and _empty_quote(body):
            # This is still invalid data, not proof of a connection or of no trades.
            if checked["normalized"]["current_date"] == now.astimezone(JST).date().isoformat():
                waiting_quote = True
            else:
                issues.add("startup_date_changed")
                blocked = True
        else:
            blocked = True
    if blocked:
        state = "blocked"
    elif waiting_quote:
        state = "waiting_for_quote"
    elif waiting_date:
        state = "waiting_for_current_date"
    else:
        state = "ready"
    return {"state": state, "issues": sorted(issues)}
