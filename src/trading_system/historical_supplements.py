"""Conservative extraction and arithmetic for source-linked supplemental observations.

These functions do not decide broker-course eligibility, historical coverage,
record dates, fractional cash payments, or portfolio booking dates.
"""

import re
import unicodedata
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from fractions import Fraction

DATE_PATTERN = r"(?:平成|令和)?(?:[0-9]{1,4}|元)年[0-9]{1,2}月[0-9]{1,2}日"
JAPANESE_DATE = re.compile(r"(平成|令和)?([0-9]{1,4}|元)年([0-9]{1,2})月([0-9]{1,2})日")


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def japanese_date(text: str) -> date:
    match = JAPANESE_DATE.fullmatch(compact(text))
    if not match:
        raise ValueError("invalid_japanese_date")
    era, year, month, day = match.groups()
    year = 1 if year == "元" else int(year)
    if year <= 0 or (era is None and year < 1000):
        raise ValueError("ambiguous_year")
    year += {"平成": 1988, "令和": 2018, None: 0}[era]
    result = date(year, int(month), int(day))
    if era == "平成" and not date(1989, 1, 8) <= result <= date(2019, 4, 30):
        raise ValueError("date_outside_era")
    if era == "令和" and result < date(2019, 5, 1):
        raise ValueError("date_outside_era")
    return result


def payment_observation(text: str) -> dict:
    """Never interpret the start of a postal collection window as bank receipt."""
    text = compact(text)
    matches = []
    for kind, pattern in (
        ("scheduled_payment_start", rf"配当(?:金)?支払開始予定日[:：]?({DATE_PATTERN})"),
        ("postal_collection_window_start", rf"払渡期間[（(]?({DATE_PATTERN})"),
    ):
        for match in re.finditer(pattern, text):
            matches.append((kind, japanese_date(match[1]).isoformat(), match[0]))
    distinct = {(kind, day) for kind, day, _ in matches}
    if len(distinct) > 1:
        raise ValueError("ambiguous_payment_dates_require_review")
    if not matches:
        return {
            "state": "no_date_extracted",
            "date_kind": None,
            "date": None,
            "amount_jpy": None,
            "evidence": [],
            "cash_booking_allowed": False,
        }
    kind, day, anchor = matches[0]
    amounts = {
        match[1] for match in re.finditer(r"期末配当金(?:は、?|\()1株につき([0-9]+)円", text)
    }
    if len(amounts) > 1:
        raise ValueError("ambiguous_dividend_amounts_require_review")
    return {
        "state": "extracted_observation",
        "date_kind": kind,
        "date": day,
        "amount_jpy": next(iter(amounts)) if amounts else None,
        "evidence": [anchor],
        "record_date": None,
        "bank_receipt_date": None,
        "cash_booking_allowed": False,
    }


def allotment_quantities(existing_shares: int, additional_per_share: Fraction) -> dict:
    """Long-share entitlement arithmetic only; a 0.05 allotment means 1.05 total."""
    if type(existing_shares) is not int or existing_shares < 0:
        raise ValueError("nonnegative_integer_existing_shares_required")
    if not isinstance(additional_per_share, Fraction) or additional_per_share <= 0:
        raise ValueError("positive_exact_additional_ratio_required")
    additional = existing_shares * additional_per_share
    whole = additional.numerator // additional.denominator
    return {
        "existing_shares": existing_shares,
        "additional_whole_shares": whole,
        "total_whole_shares": existing_shares + whole,
        "fractional_additional_shares": additional - whole,
        "cash_in_lieu": None,
    }


def quoted_commission_before_tax(
    notional: Decimal, tiers: list[tuple[Decimal | None, Decimal]]
) -> Decimal:
    """Calculate an explicitly selected quote; not a date/course selection rule.

    A finite last tier is intentional: do not invent unreported upper brackets.
    """
    if not notional.is_finite() or notional <= 0 or not tiers:
        raise ValueError("positive_notional_and_tiers_required")
    previous = Decimal(0)
    for i, (upper, fee) in enumerate(tiers):
        if not fee.is_finite() or fee < 0:
            raise ValueError("invalid_fee")
        if upper is None:
            if i != len(tiers) - 1:
                raise ValueError("unbounded_tier_must_be_last")
        elif not upper.is_finite() or upper <= previous:
            raise ValueError("tier_bounds_must_increase")
        else:
            previous = upper
    for upper, fee in tiers:
        if upper is None or notional <= upper:
            return fee
    raise ValueError("unreported_fee_bracket")


def quoted_fee_with_tax(fee: Decimal, tax_rate: Decimal) -> Decimal:
    """Explicit tax rate, floor sub-yen amounts; no automatic tax-date assumption."""
    if not fee.is_finite() or fee < 0 or not tax_rate.is_finite() or tax_rate < 0:
        raise ValueError("invalid_fee_or_tax")
    return (fee * (1 + tax_rate)).quantize(Decimal(1), rounding=ROUND_FLOOR)
