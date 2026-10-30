import copy
from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest

from trading_system.adjusted_history import ADJUSTED, RAW
from trading_system.adjusted_verify import PricePages, choose_codes, compare_rows


def comparison_row():
    result = {"Code": "01230", "Date": "2026-10-30", "ExRT": ""}
    result.update({key: "10.0" for key in (*RAW, "AdjFactor", *ADJUSTED)})
    return result


def test_api_decimal_representations_match_exactly_without_modification():
    local = comparison_row()
    api = {k: (Decimal(v) if k in (*RAW, "AdjFactor", *ADJUSTED) else v) for k, v in local.items()}
    before = copy.deepcopy(api)
    result = compare_rows([local], [api])
    assert result["state"] == "checked" and result["compared_rows"] == 1
    assert api == before


@pytest.mark.parametrize(
    ("field", "group"),
    [
        ("O", "raw_differences"),
        ("AdjFactor", "raw_differences"),
        ("ExRT", "raw_differences"),
        ("AdjC", "adjusted_differences"),
        ("AdjVo", "adjusted_differences"),
    ],
)
def test_raw_and_adjustment_differences_are_separate(field, group):
    local = comparison_row()
    api = dict(local)
    api[field] = "20"
    result = compare_rows([local], [api])
    assert result["state"] == "issues_found" and result[group] == 1
    assert result["difference_examples"][0]["field"] == field


def test_missing_key_is_not_treated_as_null_and_empty_is_not_success():
    local = comparison_row()
    api = dict(local)
    for key in (*RAW, *ADJUSTED):
        local[key] = ""
        api[key] = None
    assert compare_rows([local], [api])["state"] == "checked"
    del api["AdjC"]
    assert compare_rows([local], [api])["adjusted_differences"] == 1
    assert compare_rows([], [])["state"] == "issues_found"


def test_missing_rows_extra_rows_and_duplicates_are_not_hidden():
    local = comparison_row()
    other = dict(local)
    other["Code"] = "130A0"
    result = compare_rows([local], [other])
    assert result["missing_api_keys"] == [("01230", "2026-10-30")]
    assert result["unexpected_api_keys"] == [("130A0", "2026-10-30")]
    with pytest.raises(ValueError, match="duplicate_comparison_key"):
        compare_rows([local], [local, local])


def test_selection_is_fixed_by_event_type_not_api_outcome():
    rows = [
        {"Code": f"{i:04d}0", "ExRT": str(1 + (i % 3)), "AdjFactor": "0.5"}
        for i in range(1000, 1012)
    ]
    rows.append({"Code": "20000", "ExRT": "", "AdjFactor": "1"})
    with duckdb.connect() as db:
        db.register("prices", pa.Table.from_pylist(rows))
        first = choose_codes(db)
        db.unregister("prices")
        db.register("prices", pa.Table.from_pylist(list(reversed(rows))))
        assert choose_codes(db) == first
    assert any(s["code"] == "20000" and "no_adjustments" in s["reasons"] for s in first)
    for event in ("1", "2", "3"):
        assert sum("event_" + event + "_hash_sample" in s["reasons"] for s in first) == 2


def test_client_rejects_non_price_endpoints_before_network():
    client = PricePages("test-only")
    try:
        with pytest.raises(ValueError, match="comparison_endpoint_not_allowed"):
            client.get("/orders", {})
    finally:
        client.close()


def test_selection_excludes_codes_with_no_rows_in_comparison_period():
    rows = [
        {"Code": "60750", "Date": "2016-10-10", "ExRT": "", "AdjFactor": "1"},
        {"Code": "01230", "Date": "2026-10-30", "ExRT": "", "AdjFactor": "1"},
        {"Code": "99990", "Date": "2026-10-30", "ExRT": "1", "AdjFactor": "0.5"},
    ]
    with duckdb.connect() as db:
        db.register("prices", pa.Table.from_pylist(rows))
        selected = choose_codes(db, "2016-10-31", "2026-10-30")
    assert {r["code"] for r in selected} == {"01230", "99990"}


def test_precision_review_is_a_screening_list_and_excludes_unavailable_periods():
    from trading_system.adjusted_verify import choose_precision_codes

    rows = [
        {"Code": "01230", "Date": "2026-10-30", "AdjFactor": "0.9090909090909092"},
        {"Code": "01230", "Date": "2025-10-30", "AdjFactor": "0.9090909090909092"},
        {"Code": "130A0", "Date": "2026-10-30", "AdjFactor": "0.5"},
        {"Code": "99990", "Date": "2016-10-10", "AdjFactor": "0.3333333333333333"},
    ]
    with duckdb.connect() as db:
        db.register("prices", pa.Table.from_pylist(rows))
        result = choose_precision_codes(db, "2016-10-31", "2026-10-30")
    assert result == [
        {"code": "01230", "reasons": ["long_decimal_coefficient_review"], "event_rows": 2}
    ]
