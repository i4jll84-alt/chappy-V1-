import copy
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from trading_system.research import irbank_first5 as m


def test_not_authorized_does_not_run_or_load_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "inputs", lambda *a: pytest.fail("unauthorized research"))
    assert m.run(tmp_path) == {"state": "disabled"}


def test_rule_drift_disables_automatic_research(tmp_path):
    root = tmp_path
    spec = copy.deepcopy(m.SPEC)
    m.save(root / "acquisition-policy.json", {"first5_comparison": {"enabled": True, "spec": spec}})
    assert m.authorized(root)
    spec["fixed_shares"] = 200
    m.save(root / "acquisition-policy.json", {"first5_comparison": {"enabled": True, "spec": spec}})
    assert not m.authorized(root)


def test_missing_original_is_not_zero_return(tmp_path):
    path = tmp_path / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs/sources.json"
    m.save(path, {})
    result = m.original_sources(tmp_path, ["2016-11-17", "2016-11-18"])
    assert result["state"] == "waiting" and result["missing_days"] == 2
    assert result["metrics"] is None
    assert m.comparison_deltas({"original": result, "approx": {"state": "complete"}}) is None


def test_missing_pages_and_complete_windows_are_different(tmp_path, monkeypatch):
    data = {
        "sessions": ["2016-11-17", "2016-11-18", "2021-11-19"],
        "days": {"2016-11-18": [{"code": "72030"}]},
    }
    monkeypatch.setattr(m, "prepare_candidates", lambda *a: data)
    *_, state = m.inputs(tmp_path, tmp_path)
    assert state["remaining_codes"] == 1
    m.save(
        tmp_path / "state/first5/7203.json",
        {
            "code": "7203",
            "start": "2016-11-17",
            "end": "2021-11-19",
            "state": "complete",
            "pages": ["2021-11-19"],
        },
    )
    *_, state = m.inputs(tmp_path, tmp_path)
    assert state["state"] == "ready"
    with pytest.raises(ValueError, match="completed_first5_page_missing"):
        m.approximate_ranks(
            tmp_path, data["sessions"], data["days"], {"7203": {"pages": ["2021-11-19"]}}
        )


def test_fixed_top_five_no_missing_balance_imputation(tmp_path, monkeypatch):
    dates = ["2016-11-17", "2016-11-18"]
    pools = {dates[-1]: [{"code": f"{i:04d}0"} for i in range(1000, 1007)]}
    states = {r["code"][:-1]: {"pages": ["2021-11-19"]} for r in pools[dates[-1]]}

    class Evidence:
        def __init__(self, *a):
            self.archive = SimpleNamespace(
                read=lambda *args: ({"to": "2021-11-19"}, [{"date": "2016-11-16"}])
            )

        def read(self, code):
            if code == "1000":
                return {}, [], []
            return (
                {
                    d: {"loan_balance": 100, "stock_loan_balance": 0, "net_balance": 100}
                    for d in dates
                },
                [],
                [],
            )

    monkeypatch.setattr(m, "Evidence", Evidence)
    ranks, evidence = m.approximate_ranks(tmp_path, dates, pools, states)
    assert ranks[dates[-1]] == [f"{i:04d}0" for i in range(1001, 1006)]
    assert len(evidence["unobserved_tested"]) == 1
    assert len(evidence["selections"][0]["tested"]) == 6


def test_approximate_runs_even_when_original_source_missing(tmp_path, monkeypatch):
    root = tmp_path / "data/irbank_jsf"
    root.mkdir(parents=True)
    (root / "technical-candidates.json").write_text("{}")
    m.save(
        root / "acquisition-policy.json",
        {"first5_comparison": {"frozen_engine_sha256": {"synthetic": "synthetic"}}},
    )
    technical = {"source_sha256": {}, "feature_source_sha256": "synthetic"}
    days = ["2016-11-17", "2016-11-18", "2021-11-19"]
    monkeypatch.setattr(m, "authorized", lambda *a: True)
    monkeypatch.setattr(
        m, "inputs", lambda *a: (technical, days, {}, {}, {"state": "ready", "remaining_codes": 0})
    )
    monkeypatch.setattr(
        m,
        "original_sources",
        lambda *a: {"state": "waiting", "metrics": None, "reason": "original_missing"},
    )
    monkeypatch.setattr(m.shared, "history", lambda *a: (days, {}))
    monkeypatch.setattr(m.shared, "digest", lambda *a: "synthetic")

    @contextmanager
    def imports(*a):
        yield {}

    monkeypatch.setattr(m.shared, "imports", imports)
    monkeypatch.setattr(m, "approximate_ranks", lambda *a: ({}, {"unobserved_tested": []}))
    calls = []

    def replay(*args):
        calls.append(args[5])
        return {"state": "complete", "metrics": {}}

    monkeypatch.setattr(m, "replay_branch", replay)
    result = m.run(tmp_path)
    assert calls == ["approx"] and result["state"] == "waiting"
    assert result["deltas"] is None


def test_net_profit_deltas_are_after_costs_not_gross():
    a = {
        "net_profit": -100,
        "return_pct": -1,
        "drawdown_pct": -2,
        "annual_return_pct": -0.2,
        "pf": None,
    }
    b = {**a, "net_profit": 200, "return_pct": 2}
    branches = {
        key: {"state": "complete", "metrics": {"A0_base": value, "A0_base_slip": value}}
        for key, value in (("original", a), ("approx", b))
    }
    assert m.comparison_deltas(branches)["A0_base"]["net_profit"] == 300
    assert m.comparison_deltas(branches)["A0_base"]["pf"] is None


def test_replay_double_run_cost_summary_reuse_and_tamper(tmp_path, monkeypatch):
    project, lab = tmp_path, tmp_path / "lab"
    days = [m.shared.ANCHOR, m.shared.FIRST5_END]
    case = {
        "equity": {
            days[0]: {"nav": 3000000, "exposed": False},
            days[1]: {"nav": 3000100, "exposed": True},
        },
        "trades": [],
        "counters": {},
        "buys": [],
        "closing_positions": [],
        "pending_settlements": [],
    }
    calls = []

    def replay(*args):
        calls.append(1)
        return {"A0_base": copy.deepcopy(case), "A0_base_slip": copy.deepcopy(case)}, {
            "prices": "fixture"
        }

    monkeypatch.setattr(m.shared, "replay_once", replay)
    metrics = SimpleNamespace(
        measure=lambda *args: {
            "first5": {"coverage": "full", "opening_nav": 3000000, "ending_nav": 3000100}
        }
    )
    evidence = {"credit": {"unobserved_tested": []}}
    a = m.replay_branch(project, lab, {"structure_metrics": metrics}, days, {}, "approx", evidence)
    assert len(calls) == 2 and a["metrics"]["A0_base"]["net_profit"] == 100
    b = m.replay_branch(project, lab, {"structure_metrics": metrics}, days, {}, "approx", evidence)
    assert b["reused"] and len(calls) == 2
    from pathlib import Path

    (Path(a["output"]) / "accounts.json").write_text("{}")
    with pytest.raises(ValueError, match="first5_result_changed"):
        m.replay_branch(project, lab, {"structure_metrics": metrics}, days, {}, "approx", evidence)


def test_daily_quota_wait_still_checks_offline_comparison(tmp_path, monkeypatch):
    from trading_system.research import irbank_daily

    calls = []
    monkeypatch.setattr(irbank_daily, "inspect", lambda *a: {"state": "daily_quota_reached"})
    monkeypatch.setattr(
        irbank_daily, "safe_check", lambda p: calls.append(p) or {"state": "waiting"}
    )
    monkeypatch.setattr(irbank_daily.jsf, "run", lambda *a: pytest.fail("must not use API"))
    result = irbank_daily.run(tmp_path)
    assert calls == [tmp_path.resolve()]
    assert result["comparison"]["state"] == "waiting"


def test_absent_original_manifest_and_comparison_lock_are_safe(tmp_path, monkeypatch):
    assert m.original_sources(tmp_path, ["2016-11-17"])["state"] == "waiting"
    monkeypatch.setattr(
        m, "run", lambda *a: (_ for _ in ()).throw(ValueError("broker_state_already_open"))
    )
    assert m.safe_check(tmp_path) == {"state": "already_running"}
    assert not (tmp_path / "data/irbank_jsf/first5-comparison-error.json").exists()
