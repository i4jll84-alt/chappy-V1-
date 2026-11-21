"""Synthetic all-market aggregation, vintage and frozen-prefix checks."""

import copy
import csv
import io
import json
from decimal import Decimal

import pytest

from trading_system.research import irbank_compare as m


def body(rows):
    out = io.StringIO(newline="")
    fields = ["銘柄コード", "市場区分", *m.FIELDS, "制限措置等"]
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for code, market, loan, stock, restriction in rows:
        writer.writerow(
            {
                "銘柄コード": code,
                "市場区分": market,
                m.FIELDS[0]: loan,
                m.FIELDS[1]: stock,
                m.FIELDS[2]: int(loan) - int(stock) if loan != "" and stock != "" else "",
                "制限措置等": restriction,
            }
        )
    return out.getvalue().encode("cp932")


def row(loan, stock=0, restriction=""):
    return {
        m.FIELDS[0]: str(loan),
        m.FIELDS[1]: str(stock),
        m.FIELDS[2]: str(loan - stock),
        "制限措置等": restriction,
    }


def native_pass(before, current):
    if not before or not current or current.get("制限措置等") != "":
        return False
    a, b = m.balances(before), m.balances(current)
    return a is not None and b is not None and max(*a[:2], *b[:2]) > 0 and b[2] <= a[2]


def test_aggregate_sums_all_market_rows_and_drops_restrictions():
    rows, proof = m.aggregate(
        body(
            [
                ("7203", "東証", 734100, 100, "申込停止"),
                ("7203", "名証", 100, 0, ""),
                ("7203", "福証", 20, 0, ""),
            ]
        )
    )
    assert m.balances(rows["72030"]) == (734220, 100, 734120)
    assert rows["72030"]["制限措置等"] == ""
    assert proof["markets"] == {"東証": 1, "名証": 1, "福証": 1}


def test_unknown_component_does_not_become_zero_or_partial_sum():
    rows, proof = m.aggregate(
        body(
            [
                ("7203", "東証", 100, 0, ""),
                ("7203", "名証", "", "", ""),
                ("6758", "東証", 200, 10, ""),
            ]
        )
    )
    assert "72030" not in rows
    assert m.balances(rows["67580"]) == (200, 10, 190)
    assert proof["unobserved_codes"] == ["72030"]


def test_five_digit_preferred_share_is_preserved_without_entering_ordinary_pool():
    rows, _ = m.aggregate(body([("25935", "東証", 200, 10, ""), ("2593", "東証", 100, 20, "")]))
    assert set(rows) == {"25935", "25930"}
    assert m.balances(rows["25935"]) == (200, 10, 190)
    selected, _, _ = m.select([("25930", Decimal(100))], rows, rows, native_pass)
    assert selected == ["25930"]


@pytest.mark.parametrize("code", ["259", "259350", "25-3", "25a3"])
def test_invalid_official_code_remains_rejected(code):
    with pytest.raises(ValueError, match="invalid_official_code"):
        m.aggregate(body([(code, "東証", 100, 0, "")]))


def test_duplicate_market_rows_are_rejected():
    with pytest.raises(ValueError, match="duplicate_official"):
        m.aggregate(body([("7203", "東証", 100, 0, ""), ("7203", "東証", 100, 0, "")]))


def test_normalized_code_aliases_cannot_double_count_same_market():
    with pytest.raises(ValueError, match="duplicate_official"):
        m.aggregate(body([("2593", "東証", 100, 0, ""), ("25930", "東証", 100, 0, "")]))


def test_balance_arithmetic_and_unknown_values_remain_distinct():
    assert m.balances(None) is None
    assert m.balances({**row(100), m.FIELDS[0]: "-"}) is None
    with pytest.raises(ValueError, match="balance_arithmetic"):
        m.balances({**row(100), m.FIELDS[2]: "99"})


def test_credit_filter_precedes_top_five_and_missing_is_explicit():
    pool = [(f"{1000 + i}0", Decimal(100)) for i in range(9)]
    before = {code: row(100) for code, _ in pool}
    current = {code: row(90) for code, _ in pool}
    current["10000"] = row(101)
    current["10010"] = row(90, restriction="申込停止")
    del current["10020"]
    selected, tested, missing = m.select(pool, before, current, native_pass)
    assert selected == [f"{i}0" for i in range(1003, 1008)]
    assert len(tested) == 8 and missing == 1
    assert tested[2]["state"] == "no_data"
    assert "10080" not in selected


def test_restriction_and_vintage_changes_are_reported_without_causal_claim():
    tested = ([{"code": "72030"}], [{"code": "72030"}])
    reasons = m.change_reasons(
        tested,
        {"72030": row(200)},
        {"72030": row(100, restriction="停止")},
        {"72030": row(250)},
        {"72030": row(300)},
    )
    assert reasons == {"restriction_removed": 1, "market_or_vintage_balance_change": 1}


def test_recent_proxy_requires_later_capture_and_checks_date_and_hash(tmp_path):
    day = "2026-11-13"
    with pytest.raises(FileNotFoundError):
        m.proxy_body(tmp_path, day)
    folder = tmp_path / "data/irbank_jsf/diagnostics/jsf-20261113"
    folder.mkdir(parents=True)
    csv_path = folder / "all.csv"
    csv_path.write_bytes(body([("7203", "東証", 100, 0, "")]))
    receipt = {
        "date": day,
        "url": "https://www.taisyaku.jp/app/stock/csv",
        "sha256": m.shared.digest(csv_path),
    }
    path = folder / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    raw, refs = m.proxy_body(tmp_path, day)
    assert raw == csv_path.read_bytes() and refs[str(csv_path)] == receipt["sha256"]
    receipt["date"] = "2026-11-12"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_mismatch"):
        m.proxy_body(tmp_path, day)
    receipt["date"] = day
    path.write_text(json.dumps(receipt), encoding="utf-8")
    csv_path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash_mismatch"):
        m.proxy_body(tmp_path, day)


def prefix_fixture():
    cases, baseline, entries = {}, {"cases": {}}, {}
    for name, source in (("original", "A0_base"), ("original_slip", "A0_base_slip")):
        case = {
            "equity": {m.ANCHOR: {"nav": 3000000}, m.END: {"nav": 3100000}},
            "trades": [{"exit_date": m.END, "net_pnl": 100000}],
            "buys": [{"date": "2023-11-02", "code": "72030", "shares": 100}],
        }
        cases[name] = copy.deepcopy(case)
        baseline["cases"][source] = {
            "equity": {**case["equity"], "2026-11-20": {"nav": 3200000}},
            "trades": [*case["trades"], {"exit_date": "2026-11-20", "net_pnl": 100000}],
        }
        entries[source] = case["buys"] + [{"date": "2026-11-20", "code": "67580", "shares": 100}]
    return cases, baseline, entries


def test_original_equity_trades_and_entries_match_exact_prefix_only():
    cases, baseline, entries = prefix_fixture()
    proof = m.verify_prefix(cases, baseline, entries)
    assert proof["original"] == {"equity": 2, "trades": 1, "buys": 1}


@pytest.mark.parametrize("field", ["equity", "trades", "buys"])
def test_original_prefix_mismatch_stops_comparison(field):
    cases, baseline, entries = prefix_fixture()
    cases["original"][field] = {} if field == "equity" else []
    with pytest.raises(ValueError, match="original_prefix_mismatch"):
        m.verify_prefix(cases, baseline, entries)


def test_output_location_must_be_local_research_child(tmp_path):
    with pytest.raises(ValueError, match="output_must_be"):
        m.run(tmp_path, tmp_path / "lab", tmp_path / "production")
    with pytest.raises(ValueError, match="non_onedrive"):
        m.run(tmp_path / "OneDrive", tmp_path / "lab")


def test_comparison_reuse_requires_all_artifacts_and_same_input_identity(tmp_path):
    target = tmp_path / "study"
    assert m.cached(target, "frozen") is None
    target.mkdir()
    with pytest.raises(ValueError, match="incomplete"):
        m.cached(target, "frozen")
    names = [
        "report.json",
        "accounts.json",
        "metrics.csv",
        "deltas.csv",
        "daily-nav.csv",
        "selections.csv",
    ]
    for name in names:
        (target / name).write_bytes(b"synthetic")
    manifest = {
        "state": "complete",
        "input_sha256": "frozen",
        "outputs": {name: m.shared.digest(target / name) for name in names},
    }
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert m.cached(target, "frozen")["scope"] == "common_official_proxy"
    with pytest.raises(ValueError, match="inputs_differ"):
        m.cached(target, "other")
    (target / "daily-nav.csv").write_bytes(b"changed")
    with pytest.raises(ValueError, match="output_changed"):
        m.cached(target, "frozen")
