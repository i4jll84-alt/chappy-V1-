"""Explicit dated inputs for a conservative, new-exposure-only synthetic planner."""

from decimal import Decimal
from typing import Literal

from pydantic import Field, StrictBool, model_validator

from trading_system.backtest.models import Input, Nonnegative, Positive, Time


class CandidateSignal(Input):
    strategy: str = Field(min_length=1)
    code: str = Field(min_length=1)
    direction: int = Field(strict=True, ge=-1, le=1)
    decision_at: Time
    known_at: Time
    feature_known_at: Time
    trained_at: Time
    model_id: str = Field(min_length=1)
    source_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confidence: Decimal | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class Allocation(Input):
    strategy: str = Field(min_length=1)
    quantity: int = Field(strict=True, gt=0)


class Exposure(Input):
    code: str = Field(min_length=1)
    direction: int = Field(strict=True, ge=-1, le=1)
    quantity: int = Field(strict=True, gt=0)
    allocations: tuple[Allocation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def attribution(self):
        if self.direction == 0:
            raise ValueError("An existing exposure must be long or short")
        if sum(item.quantity for item in self.allocations) != self.quantity:
            raise ValueError("Attribution quantities must equal exposure quantity")
        if len({item.strategy for item in self.allocations}) != len(self.allocations):
            raise ValueError("Duplicate exposure strategy attribution")
        return self


class Position(Exposure):
    pass


class Reservation(Exposure):
    order_id: str = Field(min_length=1)
    planning_price: Positive
    fee_buffer: Nonnegative
    known_at: Time


class Instrument(Input):
    code: str = Field(min_length=1)
    sector: str = Field(min_length=1)
    mark_price: Positive
    mark_as_of: Time
    mark_known_at: Time
    planning_price: Positive
    rule_known_at: Time
    valid_from: Time
    valid_until: Time
    lot_size: int = Field(strict=True, gt=0)
    short_allowed: StrictBool
    short_total_capacity: int = Field(strict=True, ge=0)
    short_additional_collateral_ratio: Positive
    fee_fixed: Nonnegative
    fee_per_share: Nonnegative
    adverse_move_fraction: Decimal = Field(ge=0, allow_inf_nan=False)


class Account(Input):
    cash: Decimal = Field(allow_inf_nan=False)
    as_of: Time
    known_at: Time
    day_start_equity: Positive
    reference_equity: Positive
    source_id: str = Field(min_length=1)


class StrategyWeight(Input):
    strategy: str = Field(min_length=1)
    weight: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    priority: int = Field(strict=True, ge=0)


class Correlation(Input):
    code_a: str = Field(min_length=1)
    code_b: str = Field(min_length=1)
    correlation: Decimal = Field(ge=-1, le=1, allow_inf_nan=False)
    window_end: Time
    known_at: Time
    valid_until: Time


class Limits(Input):
    max_symbol_fraction: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    max_sector_fraction: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    max_long_fraction: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    max_short_fraction: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    max_gross_fraction: Decimal = Field(ge=0, le=2, allow_inf_nan=False)
    short_credit_limit: Nonnegative
    min_cash_ratio: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    max_daily_loss_fraction: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    max_reference_loss_fraction: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    max_stress_loss_fraction: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    correlation_threshold: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    correlated_pair_cap: Decimal = Field(ge=0, le=2, allow_inf_nan=False)
    max_evidence_age_seconds: int = Field(strict=True, gt=0)


class PlanInput(Input):
    mode: Literal["synthetic"]
    decision_at: Time
    source_label: str = Field(min_length=1)
    account: Account
    instruments: tuple[Instrument, ...]
    strategies: tuple[StrategyWeight, ...] = Field(min_length=1)
    positions: tuple[Position, ...]
    reservations: tuple[Reservation, ...]
    signals: tuple[CandidateSignal, ...]
    correlations: tuple[Correlation, ...]
    limits: Limits

    @model_validator(mode="after")
    def unique_inputs(self):
        for values, key in (
            (self.instruments, lambda x: x.code),
            (self.strategies, lambda x: x.strategy),
            (self.positions, lambda x: x.code),
            (self.reservations, lambda x: x.order_id),
            (self.signals, lambda x: (x.strategy, x.code)),
            (self.correlations, lambda x: tuple(sorted((x.code_a, x.code_b)))),
        ):
            keys = [key(item) for item in values]
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate planning input key")
        if sum((strategy.weight for strategy in self.strategies), Decimal(0)) > 1:
            raise ValueError("Fixed strategy weights may not exceed total equity")
        if any(item.code_a == item.code_b for item in self.correlations):
            raise ValueError("Correlation must describe distinct codes")
        return self
