"""Synthetic receipt, calendar-boundary and replay-integrity checks; no remote IO."""

import copy
import json
from decimal import Decimal

import pytest

from trading_system.research import irbank_replay as m


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(m.encode(value))


def receipt(root, day="2016-11-18", previous="2016-11-17", tested=None, selected=None):
    data = {
        "date": day,
        "previous": previous,
        "complete": True,
        "tested": tested
        if tested is not None
        else [{"code": "72030", "state": "pass", "sources": [{"sha256": "a" * 64}]}],
        "selected": selected if selected is not None else ["72030"],
    }
    path = root / "required_decisions" / (day + ".json")
    save(path, data)
    return path, data


def test_unchecked_audit_waits_without_loading_market_or_producing_results(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("market work must not begin before audit readiness")

    monkeypatch.setattr(m, "history", forbidden)
    result = m.run(tmp_path, tmp_path / "lab", "auto")
    assert result == m.waiting("auto", "official_audit_not_checked")
    assert not (tmp_path / "data/offline-verification").exists()


def test_missing_decision_does_not_become_zero_return(tmp_path):
    days = ["2016-11-17", "2016-11-18", "2016-11-21"]
    ranks, pins, missing = m.decisions(tmp_path, days, days[-1])
    assert ranks == pins == {}
    assert missing == [{"date": "2016-11-18", "reason": "decision_missing"}]


def test_first_anchor_decision_needs_prior_session_and_not_end_signal(tmp_path):
    days = ["2016-11-17", "2016-11-18", "2016-11-21"]
    path, _ = receipt(tmp_path)
    ranks, pins, missing = m.decisions(tmp_path, days, days[-1])
    assert ranks == {"2016-11-18": ["72030"]}
    assert pins == {str(path): m.digest(path)}
    assert not missing


def test_no_data_is_unresolved_even_if_dispatcher_marks_complete(tmp_path):
    receipt(tmp_path, tested=[{"code": "72030", "state": "no_data", "sources": []}], selected=[])
    ranks, _, missing = m.decisions(
        tmp_path, ["2016-11-17", "2016-11-18", "2016-11-21"], "2016-11-21"
    )
    assert not ranks
    assert missing[0]["reason"] == "unresolved_credit_balance"


def test_selection_must_match_passed_rank_order(tmp_path):
    receipt(
        tmp_path,
        tested=[
            {"code": "72030", "state": "pass", "sources": []},
            {"code": "67580", "state": "pass", "sources": []},
        ],
        selected=["67580", "72030"],
    )
    with pytest.raises(ValueError, match="selected_ranks_mismatch"):
        m.decisions(tmp_path, ["2016-11-17", "2016-11-18", "2016-11-21"], "2016-11-21")


def test_wrong_previous_session_cannot_cross_market_halt(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "ANCHOR", "2020-10-02")
    receipt(tmp_path, "2020-10-02", "2020-10-01")
    with pytest.raises(ValueError, match="calendar_mismatch"):
        m.decisions(tmp_path, ["2020-09-30", "2020-10-02", "2020-10-05"], "2020-10-05")


@pytest.mark.parametrize("field", ["equity", "trades", "buys", "counters", "pending_settlements"])
def test_repeat_comparison_covers_accounting_and_execution_fields(field):
    first = {
        "A0_base": {
            "equity": {},
            "trades": [],
            "buys": [],
            "counters": {},
            "pending_settlements": [],
        }
    }
    second = copy.deepcopy(first)
    second["A0_base"][field] = "changed"
    with pytest.raises(ValueError, match="replays_disagree"):
        m.verify_replays(first, second, {}, {})


def test_repeat_comparison_accepts_identical_decimal_end_state():
    one = {"pending": [(2, Decimal("100.05"))]}
    m.verify_replays(one, copy.deepcopy(one), {"raw": "abc"}, {"raw": "abc"})
    with pytest.raises(ValueError, match="replays_disagree"):
        m.verify_replays(one, one, {"raw": "abc"}, {"raw": "changed"})


def test_seam_missing_and_conflicting_balances_are_not_silently_merged(tmp_path):
    official = {"72030": {"融資残高（株）": "1,000", "貸株残高（株）": "200"}}
    missing, _ = m.check_seam(tmp_path, ["72030"], official)
    assert missing[0]["irbank"] is None
    path = tmp_path / "7203.json"
    save(path, {"days": {m.SEAM: {"loan_balance": 1000, "stock_loan_balance": 199}}})
    conflicts, _ = m.check_seam(tmp_path, ["72030"], official)
    assert conflicts[0]["official"] == (1000, 200)
    save(path, {"days": {m.SEAM: {"loan_balance": 1000, "stock_loan_balance": 200}}})
    conflicts, pins = m.check_seam(tmp_path, ["72030"], official)
    assert not conflicts and pins[str(path)] == m.digest(path)


def completed(target, identity="frozen", scope="first5"):
    target.mkdir()
    names = ("report.json", "accounts.json", "periods.csv", "portfolio.csv", "first5-nav.csv")
    for name in names:
        (target / name).write_bytes(b"fixture")
    save(
        target / "report.json",
        {"state": "complete", "scope": scope, "input_sha256": identity},
    )
    save(
        target / "manifest.json",
        {
            "state": "complete",
            "input_sha256": identity,
            "outputs": {name: m.digest(target / name) for name in names},
        },
    )


def test_completed_output_is_reusable_only_with_identical_inputs_and_artifacts(tmp_path):
    target = tmp_path / "result"
    assert m.cached_result(target, "frozen") is None
    completed(target)
    assert m.cached_result(target, "frozen")["reused"] is True
    with pytest.raises(ValueError, match="inputs_differ"):
        m.cached_result(target, "new-source")
    (target / "accounts.json").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="output_corrupt"):
        m.cached_result(target, "frozen")


@pytest.mark.parametrize("scope", ["first5", "full"])
def test_cached_completion_returns_verified_report_scope(tmp_path, scope):
    target = tmp_path / "result"
    completed(target, scope=scope)
    result = m.cached_result(target, "frozen")
    assert result["state"] == "complete"
    assert result["scope"] == scope


def test_incomplete_or_empty_manifest_cannot_be_reused(tmp_path):
    target = tmp_path / "result"
    target.mkdir()
    with pytest.raises(ValueError, match="incomplete"):
        m.cached_result(target, "same")
    save(target / "manifest.json", {"state": "complete", "input_sha256": "same", "outputs": {}})
    with pytest.raises(ValueError, match="manifest_invalid"):
        m.cached_result(target, "same")


def test_shared_daily_nav_keeps_actual_capital_and_excludes_unrequested_suffix():
    days = [m.ANCHOR, m.FIRST5_END, m.END]
    cases = {
        "A0_base": {"equity": {d: {"nav": v} for d, v in zip(days, [3e6, 2e6, 9e6])}},
        "A0_base_slip": {"equity": {d: {"nav": v} for d, v in zip(days, [3e6, 1.8e6, 8e6])}},
        "B0_base": {"equity": {d: {"nav": v} for d, v in zip(days, [3e6, 2.5e6, 7e6])}},
    }
    rows = m.daily_rows(cases)
    assert [r["date"] for r in rows] == days[:2]
    assert rows[0]["A0_B0"] == 6e6
    assert rows[1]["A0_B0"] == 4.5e6
    assert rows[1]["A0_slip_B0"] == 4.3e6


def test_encode_output_is_finite():
    assert json.loads(m.encode({"pending": Decimal("4.25")}))["pending"] == "4.25"
    with pytest.raises(ValueError):
        m.encode({"nav": float("nan")})
