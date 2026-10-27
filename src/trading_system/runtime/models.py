"""Explicit calendar coverage and immutable, paper-only operation settings."""

from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, StrictBool, model_validator

from trading_system.brokers.models import ContractInput
from trading_system.point_in_time.policy import aware_utc

JST = ZoneInfo("Asia/Tokyo")


def daytime_deadline(now: datetime) -> datetime:
    """The operator's 16:00 deadline, independent of machine timezone."""
    local = aware_utc(now).astimezone(JST)
    return datetime.combine(local.date(), time(16), tzinfo=JST)


KINDS = ("prices", "topix", "master", "financials", "quality", "features", "candidates", "report")
DATASETS = KINDS[:4]


class Day(ContractInput):
    day: date
    business: StrictBool
    opens: time = time(9)
    closes: time = time(15, 30)

    @model_validator(mode="after")
    def times(self):
        if not self.opens < self.closes < time(16):
            raise ValueError("invalid_session_times")
        return self


class Calendar(ContractInput):
    source: str = "fixed_simulated_calendar"
    known_at: datetime
    days: tuple[Day, ...]

    @model_validator(mode="after")
    def complete(self):
        aware_utc(self.known_at)
        dates = [d.day for d in self.days]
        if not dates or dates != sorted(set(dates)):
            raise ValueError("calendar_order_or_duplicate")
        if (dates[-1] - dates[0]).days + 1 != len(dates):
            raise ValueError("calendar_missing_dates")
        return self

    def at(self, day):
        result = next((d for d in self.days if d.day == day), None)
        if result is None:
            raise ValueError("calendar_coverage_missing")
        return result

    def next_business(self, day):
        for item in self.days:
            if item.day > day and item.business:
                return item.day
        raise ValueError("next_business_day_unknown")

    def previous_business(self, day):
        for item in reversed(self.days):
            if item.day < day and item.business:
                return item.day
        raise ValueError("previous_business_day_unknown")


class Settings(ContractInput):
    mode: Literal["paper"] = "paper"
    real_execution_enabled: Literal[False] = False
    first_day: date
    calendar: Calendar
    definition_version: str = Field(default="v1", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    model_version: str = "unchanged-fixed-fixture"
    retry_seconds: int = Field(default=1800, ge=1)
    max_attempts: int = Field(default=20, ge=1, le=1000)
    postprocess_budget: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def coverage(self):
        self.calendar.at(self.first_day)
        return self

    def now(self, value):
        now = aware_utc(value).astimezone(JST)
        if aware_utc(self.calendar.known_at) > now:
            raise ValueError("calendar_not_yet_known")
        self.calendar.at(now.date())
        if now.date() < self.first_day:
            raise ValueError("before_operation_origin")
        return now

    def closed_days(self, now):
        end = now.date()
        result = []
        day = self.first_day
        while day <= end:
            item = self.calendar.at(day)
            if item.business and (day < end or now.time() >= item.closes):
                result.append(day)
            day += timedelta(days=1)
        return result
