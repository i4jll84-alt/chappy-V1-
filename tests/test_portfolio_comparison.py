import copy

from trading_system.portfolio import comparison
from trading_system.portfolio.simulation_benchmark import synthetic_simulation
from trading_system.storage.store import canonical, digest


def test_comparison_preserves_capital_market_rules_and_original_weights():
    source = synthetic_simulation()
    before = source.model_dump(mode="json")
    result = comparison.compare_portfolio_strategies(source)
    assert result["comparison_complete"]
    assert result["status"] == "complete"
    assert result["input"] == before == source.model_dump(mode="json")
    assert set(result["isolated"]) == {item.strategy for item in source.strategies}
    for name, arm in result["isolated"].items():
        expected = copy.deepcopy(before)
        for session in expected["sessions"]:
            session["signals"] = [s for s in session["signals"] if s["strategy"] == name]
        assert arm["input"] == expected
        assert arm["input_hash"] == digest(canonical(expected).encode())
    assert not result["execution_enabled"]
    assert not result["historical_market_validity"]
    assert result["policy"]["selection"].startswith("none")


def test_comparison_is_reproducible_and_retains_inactive_strategy():
    source = synthetic_simulation().model_dump(mode="json")
    inactive = source["strategies"][-1]["strategy"]
    for session in source["sessions"]:
        session["signals"] = [s for s in session["signals"] if s["strategy"] != inactive]
    result = comparison.compare_portfolio_strategies(source)
    assert result == comparison.compare_portfolio_strategies(source)
    assert inactive in result["isolated"]
    arm = result["isolated"][inactive]
    assert arm["status"] == "complete"
    assert not arm["orders"]
    assert not arm["positions"]


def test_partial_failure_cannot_be_reported_as_completed_comparison(monkeypatch):
    source = synthetic_simulation()
    calls = []

    def fail_one_arm(arm):
        calls.append(arm)
        return {"status": "blocked" if len(calls) == 2 else "complete", "evidence": len(calls)}

    monkeypatch.setattr(comparison, "run_portfolio_simulation", fail_one_arm)
    result = comparison.compare_portfolio_strategies(source)
    assert len(calls) == 1 + len(source.strategies)
    assert not result["comparison_complete"]
    assert result["status"] == "blocked"
    first = sorted(s.strategy for s in source.strategies)[0]
    assert result["blocked_arms"] == ["isolated:" + first]
    assert len(result["isolated"]) == len(source.strategies)
