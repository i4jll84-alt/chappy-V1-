"""Explicit immutable mock account, limits and order evidence."""

from decimal import Decimal
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, model_validator

from trading_system.backtest.models import Input, Nonnegative, Positive, Time


class OrderIntent(Input):
    client_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    side: Literal["buy", "sell", "short", "cover"]
    quantity: int = Field(strict=True, gt=0)
    limit_price: Positive
    decision_at: Time
    expires_at: Time
    strategy: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validity(self):
        if self.expires_at <= self.decision_at:
            raise ValueError("Order expiry must follow decision")
        return self


class MockRule(Input):
    code: str = Field(min_length=1)
    mark: Positive
    known_at: Time
    valid_from: Time
    valid_until: Time
    lot_size: int = Field(strict=True, gt=0)
    tick_size: Positive
    lower: Positive
    upper: Positive
    can_buy: StrictBool
    can_sell: StrictBool
    short_allowed: StrictBool
    short_capacity: int = Field(strict=True, ge=0)
    additional_collateral: Positive
    fee_fixed: Nonnegative
    fee_per_share: Nonnegative

    @model_validator(mode="after")
    def valid_rule(self):
        if self.valid_from >= self.valid_until or self.lower > self.upper:
            raise ValueError("Invalid mock rule interval/band")
        return self


class MockConfig(Input):
    mode: Literal["mock"] = "mock"
    initial_cash: Positive
    evidence_at: Time
    max_evidence_age_seconds: int = Field(strict=True, ge=0)
    initial_positions: dict[str, StrictInt]
    rules: tuple[MockRule, ...] = Field(min_length=1)
    max_order_quantity: int = Field(strict=True, gt=0)
    max_order_value: Positive
    max_position_quantity: int = Field(strict=True, gt=0)
    max_gross_value: Positive
    short_credit_limit: Nonnegative
    cash_floor: Nonnegative
    day_start_equity: Positive
    max_daily_loss_fraction: Decimal = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def unique_rules(self):
        codes = {r.code for r in self.rules}
        if len(codes) != len(self.rules) or not set(self.initial_positions) <= codes:
            raise ValueError("Duplicate/missing mock rule")
        if any(
            isinstance(q, bool) or not isinstance(q, int) for q in self.initial_positions.values()
        ):
            raise ValueError("Positions require strict integer quantities")
        return self
