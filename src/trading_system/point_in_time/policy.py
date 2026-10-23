"""Observed-time policy. Never reconstruct historical publication from a target date."""

from datetime import datetime, timedelta, timezone

RULE = "observed_at_plus_1_second_v1"


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamps are required")
    return value.astimezone(timezone.utc)


def available_at(observed_at: datetime) -> datetime:
    return aware_utc(observed_at) + timedelta(seconds=1)
