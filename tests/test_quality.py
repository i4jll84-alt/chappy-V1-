import copy

import pytest

from trading_system.validation.prices import FIELDS, validate_price


@pytest.fixture
def row():
    return {
        "Date": "2026-10-23",
        "Code": "186A0",
        "O": 100,
        "H": 110,
        "L": 90,
        "C": 105,
        "Vo": 1000,
        "Va": 100000,
        "AdjFactor": 0.5,
        "AdjO": 25,
        "AdjH": 27.5,
        "AdjL": 22.5,
        "AdjC": 26.25,
        "AdjVo": 4000,
    }


def test_valid_cumulative_adjustment_and_unknown_preserved(row):
    row["Unknown"] = {"nested": [1]}
    before = copy.deepcopy(row)
    assert validate_price(row, "186A") == []
    assert row == before


def test_no_trades_is_info(row):
    for field in ("O", "H", "L", "C", "Vo", "Va", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo"):
        row[field] = None
    assert [issue["rule"] for issue in validate_price(row)] == ["no_trades"]
    assert validate_price(row)[0]["severity"] == "info"


def test_partial_null_and_adjusted_parity(row):
    row["C"] = None
    rules = {issue["rule"] for issue in validate_price(row)}
    assert {"partial_null", "null_parity"} <= rules
    assert "no_trades" not in rules


@pytest.mark.parametrize("field", FIELDS)
def test_missing_is_distinct(row, field):
    del row[field]
    assert {"severity": "error", "rule": "missing_field", "field": field} in validate_price(row)


@pytest.mark.parametrize("value", [-1, 0, float("nan"), float("inf"), -float("inf"), True, "100"])
@pytest.mark.parametrize("field", ["O", "Vo", "Va", "AdjFactor", "AdjC", "AdjVo"])
def test_invalid_numbers(row, field, value):
    row[field] = value
    assert any(i["rule"] == "invalid_number" and i["field"] == field for i in validate_price(row))


@pytest.mark.parametrize("value", ["2026-1-01", "2026-02-30", "20261023", None, 20261023])
def test_invalid_date(row, value):
    row["Date"] = value
    assert any(i["rule"] == "invalid_date" for i in validate_price(row))


@pytest.mark.parametrize(
    "code,expected",
    [
        ("186A0", "186A"),
        ("13010", "1301"),
        ("1A3B0", "1A3B"),
        ("186A", "186A0"),
        ("186A1", "186A1"),
        ("1ABCZ", "1ABCZ"),
    ],
)
def test_alpha_and_numeric_codes(row, code, expected):
    row["Code"] = code
    assert validate_price(row, expected) == []


@pytest.mark.parametrize("code", ["186a0", "186A00", " 186A0", "A1860", 1860, None])
def test_bad_codes(row, code):
    row["Code"] = code
    assert any(i["rule"] == "invalid_code" for i in validate_price(row))


def test_wrong_expected_code(row):
    assert any(i["rule"] == "code_mismatch" for i in validate_price(row, "1301"))


def test_five_character_suffix_is_not_rewritten(row):
    row["Code"] = "186A1"
    assert any(i["rule"] == "code_mismatch" for i in validate_price(row, "186A"))


@pytest.mark.parametrize("field", ["H", "AdjH"])
def test_ohlc_order(row, field):
    row[field] = 1
    assert any(i["rule"] == "ohlc_order" for i in validate_price(row))
