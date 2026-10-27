"""Broker-neutral, account-scoped contracts; unknown evidence stays unknown."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, StrictBool, model_validator

from trading_system.backtest.models import Input, Nonnegative, Positive, Time
from trading_system.execution.models import OrderIntent

Broker = Literal["rakuten", "tachibana", "mufg_esmart"]
Credit = Literal["standard", "general"]
Action = Literal["cash_buy", "cash_sell", "margin_short", "margin_cover"]
SIDES = {"cash_buy": "buy", "cash_sell": "sell", "margin_short": "short", "margin_cover": "cover"}


class ContractInput(Input):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class Account(ContractInput):
    broker: Broker
    account_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    mode: Literal["paper"] = "paper"


class OrderRequest(ContractInput):
    internal_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)
    account: Account
    code: str = Field(min_length=1, max_length=32)
    action: Action
    credit: Credit | None = None
    position_id: str | None = None
    quantity: int = Field(strict=True, gt=0)
    order_type: Literal["limit"] = "limit"
    limit_price: Positive
    decision_at: Time
    expires_at: Time
    strategy: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_request(self):
        if self.expires_at <= self.decision_at:
            raise ValueError("invalid_expiry")
        margin = self.action in {"margin_short", "margin_cover"}
        if margin != (self.credit is not None):
            raise ValueError("explicit_credit_type_required_only_for_margin")
        if (self.action == "margin_cover") != (self.position_id is not None):
            raise ValueError("position_id_required_only_for_cover")
        return self

    def intent(self):
        return OrderIntent(
            client_id=self.internal_id,
            code=self.code,
            side=SIDES[self.action],
            quantity=self.quantity,
            limit_price=self.limit_price,
            decision_at=self.decision_at,
            expires_at=self.expires_at,
            strategy=self.strategy,
            source_hash=self.source_hash,
        )


class Fill(ContractInput):
    account: Account
    internal_id: str
    broker_order_id: str
    fill_id: str
    quantity: int = Field(strict=True, gt=0)
    price: Positive
    fee: Nonnegative
    evidence: Literal["simulated", "observed"]


class Order(ContractInput):
    request: OrderRequest
    broker_order_id: str | None
    state: Literal[
        "PREPARED",
        "DISPATCHING",
        "UNKNOWN_SUBMIT",
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCEL_REQUESTED",
        "UNKNOWN_CANCEL",
        "CANCELLED",
        "REJECTED",
    ]
    filled_quantity: int = Field(strict=True, ge=0)
    fills: tuple[Fill, ...] = ()


class Position(ContractInput):
    account: Account
    position_id: str
    code: str
    quantity: int = Field(strict=True, gt=0)
    side: Literal["long", "short"]
    credit: Credit | None
    evidence: Literal["simulated", "observed"]


class Balance(ContractInput):
    account: Account
    cash: Decimal = Field(allow_inf_nan=False)
    free_cash: Nonnegative
    short_credit_remaining: Nonnegative
    evidence: Literal["simulated", "observed"]


class Quote(ContractInput):
    code: str
    price: Positive
    known_at: Time
    evidence: Literal["simulated", "observed"]
    source_updated_at: Time | None = None
    freshness_verified: StrictBool = False


class Level(ContractInput):
    price: Positive
    quantity: int = Field(strict=True, gt=0)


class Board(ContractInput):
    code: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    known_at: Time
    evidence: Literal["simulated", "observed"]


class ShortAvailability(ContractInput):
    code: str
    credit: Credit
    allowed: StrictBool | None
    quantity: int | None = Field(default=None, strict=True, ge=0)
    known_at: Time
    valid_until: Time
    evidence: Literal["simulated", "observed", "unknown"]


class CostValue(ContractInput):
    value: Nonnegative | None = None
    evidence: Literal["observed", "estimated", "unknown"]
    unit: str

    @model_validator(mode="after")
    def explicit_unknown(self):
        if (self.evidence == "unknown") != (self.value is None):
            raise ValueError("unknown_cost_requires_null")
        return self


class Costs(ContractInput):
    code: str
    commission_fixed: CostValue
    commission_per_share: CostValue
    borrow_annual_rate: CostValue
    reverse_fee_per_share_day: CostValue
    spread: CostValue
    slippage: CostValue
    lot_size: int = Field(strict=True, gt=0)
    tick_size: Positive
    lower: Positive
    upper: Positive
    known_at: Time
    valid_until: Time


class Connection(ContractInput):
    account: Account
    state: Literal[
        "offline", "paper_ready", "reconciliation_required", "disabled", "not_implemented"
    ]
    real_execution_enabled: Literal[False] = False
    live_verified: Literal[False] = False


class SessionWindow(ContractInput):
    opens_at: Time
    closes_at: Time

    @model_validator(mode="after")
    def valid_window(self):
        if self.opens_at >= self.closes_at:
            raise ValueError("invalid_session_window")
        return self


class PaperMarket(ContractInput):
    """An explicit simulation fixture, never an inferred historical calendar."""

    evidence: Literal["simulated"] = "simulated"
    sessions: tuple[SessionWindow, ...]
    quotes: tuple[Quote, ...] = ()
    boards: tuple[Board, ...] = ()
    shorts: tuple[ShortAvailability, ...] = ()
    max_age_seconds: int = Field(strict=True, ge=0, le=3600)

    @model_validator(mode="after")
    def unique_evidence(self):
        for values, keys in (
            (self.quotes, lambda x: x.code),
            (self.boards, lambda x: x.code),
            (self.shorts, lambda x: (x.code, x.credit)),
        ):
            if len({keys(v) for v in values}) != len(values):
                raise ValueError("duplicate_market_evidence")
            if any(v.evidence != "simulated" for v in values):
                raise ValueError("paper_fixture_requires_simulated_evidence")
        windows = sorted(self.sessions, key=lambda x: x.opens_at)
        if any(a.closes_at > b.opens_at for a, b in zip(windows, windows[1:])):
            raise ValueError("overlapping_sessions")
        return self

    def require_open(self, now: datetime):
        if not any(w.opens_at <= now < w.closes_at for w in self.sessions):
            raise ValueError("closed_or_unknown_session")


def from_intent(intent: OrderIntent, account: Account, *, credit=None, position_id=None):
    """Preserve strategy output; account and margin allocation are execution concerns."""
    return OrderRequest(
        internal_id=intent.client_id,
        account=account,
        code=intent.code,
        action={v: k for k, v in SIDES.items()}[intent.side],
        credit=credit,
        position_id=position_id,
        quantity=intent.quantity,
        limit_price=intent.limit_price,
        decision_at=intent.decision_at,
        expires_at=intent.expires_at,
        strategy=intent.strategy,
        source_hash=intent.source_hash,
    )
