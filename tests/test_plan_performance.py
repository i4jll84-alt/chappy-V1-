from decimal import Decimal

import pytest

from trading_system.plan_performance import performance, profit_display, profit_tag


@pytest.mark.parametrize(
    "nav,capital,label,tag",
    [
        ("1010000", "1000000", "+10,000円（+1.00%）", "gain"),
        ("3030000", "3000000", "+30,000円（+1.00%）", "gain"),
        ("990000", "1000000", "-10,000円（-1.00%）", "loss"),
        ("1000000", "1000000", "0円（0.00%）", "neutral"),
        ("0", "1000000", "-1,000,000円（-100.00%）", "loss"),
        ("1000000.25", "1000000", "+0.25円（+0.00%）", "gain"),
        (None, "1000000", "—", "neutral"),
        ("NaN", "1000000", "—", "neutral"),
        ("Infinity", "1000000", "—", "neutral"),
        ("100", None, "—", "neutral"),
        ("100", 0, "—", "neutral"),
        ("100", -1, "—", "neutral"),
        (True, "1000000", "—", "neutral"),
    ],
)
def test_cost_based_returns_signed_labels_and_missing_values(nav, capital, label, tag):
    net, percent = performance(nav, capital)
    assert profit_display(net, percent) == label
    assert profit_tag(net) == tag


def test_holding_return_retains_purchase_cost_instead_of_using_raw_entry_quote():
    net, percent = performance(Decimal("110100"), Decimal("100060"))
    assert net == Decimal("10040")
    assert profit_display(net, percent) == "+10,040円（+10.03%）"
