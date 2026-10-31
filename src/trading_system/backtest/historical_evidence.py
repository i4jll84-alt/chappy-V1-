"""Evidence contracts for preparing historical inputs; not an execution adapter."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Literal

from trading_system.point_in_time.policy import available_at, aware_utc

JSF_POLICY_URL = "https://www.taisyaku.jp/about/backwardation/"
JSF_POLICY = "jsf_total_rate_and_no_charge_marker_v1"
MISSING = {"", "-", "―", "－"}


@dataclass(frozen=True)
class EvidenceTime:
    observed_at: datetime
    source_published_at: datetime | None = None
    recorded_available_at: datetime | None = None

    def __post_init__(self):
        for value in (
            self.observed_at,
            self.source_published_at,
            self.recorded_available_at,
        ):
            if value is not None:
                aware_utc(value)
        if self.recorded_available_at is not None:
            if aware_utc(self.recorded_available_at) < available_at(self.observed_at):
                raise ValueError("available_before_acquisition_completed_plus_one_second")

    @property
    def usable_at(self) -> datetime:
        return max(
            [
                available_at(self.observed_at),
                *(
                    aware_utc(t)
                    for t in (self.source_published_at, self.recorded_available_at)
                    if t
                ),
            ]
        )

    def require_known(self, decision_at: datetime) -> None:
        if aware_utc(decision_at) < self.usable_at:
            raise ValueError("evidence_not_available_at_decision")


def prepared_available_at(completed_at: datetime, sources: list[EvidenceTime]) -> datetime:
    if not sources:
        raise ValueError("source_evidence_required")
    return max(available_at(completed_at), *(source.usable_at for source in sources))


def parse_reverse_fee(raw: str) -> tuple[str, Decimal | None]:
    """JSF published yen rate already includes premium days. Preserve nonnumeric states."""
    value = raw.strip()
    if value in {"*****", "＊＊＊＊＊"}:
        return "not_incurred", None
    if value in MISSING:
        return "missing", None
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid_reverse_fee") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid_reverse_fee")
    return "numeric", amount


@dataclass(frozen=True)
class ReverseFeeObservation:
    application_date: date
    raw: str
    evidence: EvidenceTime
    policy_evidence: EvidenceTime

    def amount(self, *, assessed_shares: int, accounting_at: datetime) -> Decimal:
        """Caller supplies eligible shares for THIS application date, not loan inventory.

        No day multiplier: a multi-day published total must be charged exactly once.
        Portfolio accrual timing/eligibility are deliberately not inferred here.
        """
        if type(assessed_shares) is not int or assessed_shares < 0:
            raise ValueError("nonnegative_integer_assessed_shares_required")
        self.evidence.require_known(accounting_at)
        self.policy_evidence.require_known(accounting_at)
        state, rate = parse_reverse_fee(self.raw)
        if state == "missing":
            raise ValueError("reverse_fee_missing")
        if state == "not_incurred":
            return Decimal(0)
        return rate * assessed_shares


@dataclass(frozen=True)
class SplitRatio:
    kind: Literal["split", "consolidation"]
    before: Fraction
    after: Fraction

    def __post_init__(self):
        if self.before <= 0 or self.after <= 0:
            raise ValueError("positive_ratio_required")
        if self.kind == "split":
            if self.after <= self.before:
                raise ValueError("split_direction_conflict")
        elif self.kind == "consolidation":
            if self.after >= self.before:
                raise ValueError("consolidation_direction_conflict")
        else:
            raise ValueError("unsupported_ratio_kind")

    @property
    def price_multiplier(self) -> Fraction:
        return self.before / self.after

    @property
    def quantity_multiplier(self) -> Fraction:
        return self.after / self.before


def ordinary_share_candidate(code: str, product_category: str) -> bool:
    """Classification only; not listing, liquidity, lot-size or tradability approval."""
    return product_category == "011" and len(code) == 5 and code.endswith("0")
