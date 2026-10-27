"""Compare combined and isolated strategies without reallocating their budgets."""

from trading_system.portfolio.simulation import run_portfolio_simulation
from trading_system.portfolio.simulation_models import SimulationInput
from trading_system.storage.store import canonical, digest


def compare_portfolio_strategies(source: SimulationInput | dict) -> dict:
    """Run every declared strategy, including inactive ones; never select a winner."""
    source = SimulationInput.model_validate(source)
    original = source.model_dump(mode="json")
    combined = run_portfolio_simulation(source)
    isolated = {}
    for strategy in sorted(item.strategy for item in source.strategies):
        # Rebuild from validated immutable source for each arm. Never reuse balances.
        arm = source.model_dump(mode="json")
        for session in arm["sessions"]:
            session["signals"] = [
                signal for signal in session["signals"] if signal["strategy"] == strategy
            ]
        isolated[strategy] = run_portfolio_simulation(arm)
    blocked = (["combined"] if combined["status"] != "complete" else []) + [
        "isolated:" + name for name, result in isolated.items() if result["status"] != "complete"
    ]
    result = {
        "comparison_version": "synthetic_fixed_budget_isolation_v1",
        "mode": "synthetic",
        "status": "complete" if not blocked else "blocked",
        "comparison_complete": not blocked,
        "execution_enabled": False,
        "historical_market_validity": False,
        "policy": {
            "initial_cash": "identical_independent_accounts",
            "strategy_weights": "original_unchanged_no_redistribution",
            "isolated_change": "remove_other_strategy_signals_only",
            "selection": "none_all_declared_strategies_retained",
            "interpretation": "fixed_budget_isolation_not_full_capital_standalone",
        },
        "input": original,
        "input_hash": digest(canonical(original).encode()),
        "combined": combined,
        "isolated": isolated,
        "blocked_arms": blocked,
    }
    result["result_hash"] = digest(canonical(result).encode())
    return result
