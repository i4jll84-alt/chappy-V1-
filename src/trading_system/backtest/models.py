"""Explicit synthetic assumptions; no historical rule or fee defaults are inferred."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, model_validator

from trading_system.point_in_time.policy import aware_utc

Time = Annotated[datetime, AfterValidator(aware_utc)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bar(Input):
    code: str = Field(min_length=1)
    open: Positive | None
    close: Positive
    close_at: Time
    known_at: Time

    @model_validator(mode="after")
    def availability(self):
        if self.known_at < self.close_at:
            raise ValueError("Closing data cannot be known before close")
        return self


class Rule(Input):
    code: str
    known_at: Time
    lot_size: int = Field(strict=True, gt=0)
    tick_size: Positive
    limit_lower: Positive
    limit_upper: Positive
    opening_executable_quantity: int = Field(strict=True, ge=0)
    can_buy: StrictBool
    can_sell: StrictBool
    short_allowed: StrictBool
    short_inventory: int = Field(strict=True, ge=0)
    commission_rate: Nonnegative
    minimum_commission: Nonnegative
    slippage_ticks: int = Field(strict=True, ge=0)
    borrow_annual_rate: Nonnegative
    borrow_day_count: int = Field(strict=True, gt=0)
    reverse_fee_per_share_per_day: Nonnegative
    margin_ratio: Decimal = Field(ge=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def bands(self):
        if self.limit_lower > self.limit_upper:
            raise ValueError("Price bands reversed")
        return self


class Session(Input):
    date: date
    open_at: Time
    decision_at: Time
    bars: tuple[Bar, ...]
    rules: tuple[Rule, ...]
    universe: tuple[str, ...]
    universe_known_at: Time
    benchmark_close: Positive

    @model_validator(mode="after")
    def timing(self):
        if any(
            moment.astimezone(ZoneInfo("Asia/Tokyo")).date() != self.date
            for moment in (self.open_at, self.decision_at, *(b.close_at for b in self.bars))
        ):
            raise ValueError("Session date must match its Japan trading timestamps")
        if self.open_at >= self.decision_at:
            raise ValueError("Decision must follow opening")
        codes = [bar.code for bar in self.bars]
        rules = [rule.code for rule in self.rules]
        if len(codes) != len(set(codes)) or len(rules) != len(set(rules)):
            raise ValueError("Duplicate session code")
        if set(codes) != set(rules) or not set(self.universe) <= set(codes):
            raise ValueError("Session bars/rules/universe do not match")
        if len(self.universe) != len(set(self.universe)):
            raise ValueError("Duplicate universe code")
        if self.universe_known_at > self.decision_at:
            raise ValueError("Universe unavailable at decision")
        if any(rule.known_at > self.open_at for rule in self.rules):
            raise ValueError("Execution assumptions unavailable at opening")
        if any(not self.open_at < bar.close_at <= self.decision_at for bar in self.bars):
            raise ValueError("Bar close must be after open and before decision")
        return self


class Action(Input):
    kind: Literal["split", "dividend", "rights_cash"]
    code: str
    effective_date: date
    known_at: Time
    ratio: Positive | None = None
    amount_per_share: Nonnegative | None = None
    pay_date: date | None = None
    cash_in_lieu_price: Positive | None = None

    @model_validator(mode="after")
    def terms(self):
        if self.kind == "split":
            if self.ratio is None or self.amount_per_share is not None or self.pay_date is not None:
                raise ValueError("Split requires ratio only")
        elif (
            self.amount_per_share is None
            or self.pay_date is None
            or self.pay_date < self.effective_date
            or self.ratio is not None
            or self.cash_in_lieu_price is not None
        ):
            raise ValueError("Cash action requires amount and nonpast pay date")
        return self


class Strategy(Input):
    quantity: int = Field(strict=True, gt=0)
    holding_sessions: int = Field(strict=True, ge=1, le=60)
    max_allocation_fraction: Decimal = Field(gt=0, le=1, allow_inf_nan=False)


class Signal(Input):
    code: str
    decision_at: Time
    known_at: Time
    feature_cutoff_at: Time
    trained_at: Time
    direction: int = Field(strict=True, ge=-1, le=1)
    model_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def evidence(self):
        if not self.trained_at <= self.known_at <= self.decision_at:
            raise ValueError("Prediction training/availability must precede decision")
        if self.feature_cutoff_at > self.known_at:
            raise ValueError("Prediction cannot precede feature availability")
        return self


class Scenario(Input):
    mode: Literal["synthetic"]
    source_label: str = Field(min_length=1)
    initial_cash: Positive
    declared_codes: tuple[str, ...] = Field(min_length=1)
    strategy: Strategy
    risk_free_annual_rate: Decimal = Field(allow_inf_nan=False)
    sessions_per_year: int = Field(strict=True, gt=0)
    sessions: tuple[Session, ...] = Field(min_length=2)
    actions: tuple[Action, ...]
    signal_policy: Literal["momentum", "supplied"] = "momentum"
    signals: tuple[Signal, ...] = ()

    @model_validator(mode="after")
    def chronology(self):
        decisions = {session.decision_at: session for session in self.sessions}
        if self.signal_policy == "momentum" and self.signals:
            raise ValueError("Supplied signals require the supplied signal policy")
        if len({(signal.code, signal.decision_at) for signal in self.signals}) != len(self.signals):
            raise ValueError("Duplicate prediction signal")
        for signal in self.signals:
            session = decisions.get(signal.decision_at)
            if session is None or signal.code not in session.universe:
                raise ValueError("Prediction signal must match a declared decision universe")
        for first, second in zip(self.sessions, self.sessions[1:]):
            if first.date >= second.date or first.decision_at >= second.open_at:
                raise ValueError("Sessions must be strictly chronological and unique")
        if len(self.declared_codes) != len(set(self.declared_codes)):
            raise ValueError("Duplicate declared code")
        if any(
            set(bar.code for bar in session.bars) != set(self.declared_codes)
            for session in self.sessions
        ):
            raise ValueError("Every session must explicitly cover the declared synthetic universe")
        dates = {session.date for session in self.sessions}
        codes = {bar.code for session in self.sessions for bar in session.bars}
        for action in self.actions:
            if action.effective_date not in dates or action.code not in codes:
                raise ValueError("Action requires a supported session and code")
            session = next(s for s in self.sessions if s.date == action.effective_date)
            if action.known_at > session.open_at:
                raise ValueError("Action terms unavailable at effective opening")
            if action.pay_date is not None and action.pay_date not in dates:
                raise ValueError("Cash action payment must occur in supplied sessions")
        if any(
            a.kind == "split"
            and b.kind != "split"
            and a.code == b.code
            and a.effective_date == b.effective_date
            for a in self.actions
            for b in self.actions
        ):
            raise ValueError("Concurrent split and cash entitlement is unsupported")
        if len({(a.kind, a.code, a.effective_date) for a in self.actions}) != len(self.actions):
            raise ValueError("Duplicate corporate action")
        return self
