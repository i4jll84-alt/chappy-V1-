from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from trading_system.historical_supplements import (
    allotment_quantities,
    japanese_date,
    payment_observation,
    quoted_commission_before_tax,
    quoted_fee_with_tax,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("平成２８年１２月１日", date(2016, 12, 1)),
        ("令和元年７月２３日", date(2019, 7, 23)),
        ("2026 年 4 月 1 日", date(2026, 4, 1)),
    ],
)
def test_era_dates(raw, expected):
    assert japanese_date(raw) == expected


@pytest.mark.parametrize(
    "raw", ["28年12月1日", "平成31年5月1日", "令和元年4月30日", "2026年2月30日", "令和0年5月1日"]
)
def test_ambiguous_or_invalid_dates_fail(raw):
    with pytest.raises(ValueError):
        japanese_date(raw)


def test_forecast_and_collection_window_are_not_bank_receipt():
    a = payment_observation("配当支払開始予定日 平成28年12月１日")
    b = payment_observation(
        "期末配当金は、１株につき70円。払渡期間（2025年７月31日から2025年８月29日まで）"
    )
    assert a["date_kind"] == "scheduled_payment_start" and a["amount_jpy"] is None
    assert b["date_kind"] == "postal_collection_window_start" and b["amount_jpy"] == "70"
    assert b["date"] == "2025-07-31" and b["bank_receipt_date"] is None
    assert not a["cash_booking_allowed"] and not b["cash_booking_allowed"]


def test_other_dates_and_ordinary_plus_commemorative_amount_not_confused():
    text = "報告書提出予定日2025年8月1日 配当支払開始予定日―"
    assert payment_observation(text)["state"] == "no_date_extracted"
    text = "期末配当金は１株につき普通配当65円に記念配当5円を加え合計70円。"
    text += "期末配当金は、１株につき70円。払渡期間（2025年7月31日から2025年8月29日まで）"
    assert payment_observation(text)["amount_jpy"] == "70"


def test_multiple_payment_dates_stop():
    with pytest.raises(ValueError, match="ambiguous_payment"):
        payment_observation("配当支払開始予定日2025年7月1日 配当支払開始予定日2025年7月2日")


@pytest.mark.parametrize(
    "shares,increment,total,fraction",
    [
        (100, Fraction(1, 20), 105, Fraction(0)),
        (101, Fraction(1, 20), 106, Fraction(1, 20)),
        (100, Fraction(1, 5), 120, Fraction(0)),
        (1, Fraction(1, 10), 1, Fraction(1, 10)),
    ],
)
def test_free_allotment_is_additional_not_replacement(shares, increment, total, fraction):
    result = allotment_quantities(shares, increment)
    assert result["total_whole_shares"] == total
    assert result["fractional_additional_shares"] == fraction
    assert result["cash_in_lieu"] is None


@pytest.mark.parametrize(
    "shares,ratio",
    [(True, Fraction(1, 20)), (-1, Fraction(1, 20)), (100, 0.05), (100, Fraction(0))],
)
def test_allotment_rejects_ambiguous_inputs(shares, ratio):
    with pytest.raises(ValueError):
        allotment_quantities(shares, ratio)


def test_quoted_bracket_boundaries_and_explicit_tax():
    tiers = [(Decimal(100000), Decimal(139)), (Decimal(200000), Decimal(185))]
    assert quoted_commission_before_tax(Decimal(100000), tiers) == 139
    assert quoted_commission_before_tax(Decimal(100001), tiers) == 185
    with pytest.raises(ValueError, match="unreported"):
        quoted_commission_before_tax(Decimal(200001), tiers)
    assert quoted_fee_with_tax(Decimal(139), Decimal(".08")) == 150
    assert quoted_fee_with_tax(Decimal(185), Decimal(".08")) == 199
    assert quoted_fee_with_tax(Decimal(105), Decimal(".1")) == 115


@pytest.mark.parametrize(
    "tiers",
    [
        [(None, Decimal(0)), (Decimal(100), Decimal(1))],
        [(Decimal(200), Decimal(1)), (Decimal(100), Decimal(1))],
        [(None, Decimal("-1"))],
    ],
)
def test_invalid_fee_tables_fail(tiers):
    with pytest.raises(ValueError):
        quoted_commission_before_tax(Decimal(1), tiers)
