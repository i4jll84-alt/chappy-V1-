import copy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.backtest.benchmark import synthetic_scenario
from trading_system.portfolio.benchmark import synthetic_plan
from trading_system.portfolio.comparison import compare_portfolio_strategies
from trading_system.portfolio.simulation import run_portfolio_simulation


def accounting_source(direction):
    market = synthetic_scenario().model_dump(mode="json")
    code = market["declared_codes"][0]
    market["declared_codes"] = [code]
    market["sessions"] = market["sessions"][:4]
    template = synthetic_plan().model_dump(mode="json")
    frames = []
    for index, session in enumerate(market["sessions"]):
        session["bars"] = session["bars"][:1]
        session["rules"] = session["rules"][:1]
        session["universe"] = [code]
        session["bars"][0].update(open="100", close="100")
        session["rules"][0].update(
            commission_rate="0",
            minimum_commission="3",
            slippage_ticks=0,
            borrow_annual_rate="0.365",
            reverse_fee_per_share_per_day="0.01",
        )
        instrument = copy.deepcopy(template["instruments"][0])
        instrument.update(
            code=code,
            mark_price="100",
            planning_price="105",
            short_additional_collateral_ratio="1",
            fee_fixed="3",
            fee_per_share="0",
            rule_known_at=session["open_at"],
            valid_from=session["open_at"],
            valid_until=(
                datetime.fromisoformat(session["open_at"]) + timedelta(days=1)
            ).isoformat(),
        )
        opening = instrument | {
            "mark_as_of": session["open_at"],
            "mark_known_at": session["open_at"],
        }
        closing = instrument | {
            "mark_as_of": session["bars"][0]["close_at"],
            "mark_known_at": session["bars"][0]["known_at"],
        }
        signals = []
        if index == 0:
            for strategy in ("A", "B"):
                signals.append(
                    template["signals"][0]
                    | {
                        "strategy": strategy,
                        "code": code,
                        "direction": direction,
                        "decision_at": session["decision_at"],
                        "known_at": session["decision_at"],
                        "feature_known_at": session["bars"][0]["known_at"],
                        "trained_at": session["open_at"],
                    }
                )
        frames.append(
            {
                "decision_at": session["decision_at"],
                "opening_instruments": [opening],
                "decision_instruments": [closing],
                "opening_correlations": [],
                "decision_correlations": [],
                "signals": signals,
            }
        )
    limits = template["limits"] | {
        "max_symbol_fraction": "0.8",
        "max_sector_fraction": "0.8",
        "max_long_fraction": "0.8",
        "max_short_fraction": "0.8",
        "short_credit_limit": "100000",
        "min_cash_ratio": "0.1",
    }
    return {
        "mode": "synthetic",
        "source_label": "independent_flat_price_accounting_anchor",
        "market": market,
        "strategies": [
            {"strategy": "A", "weight": "0.11", "priority": 1, "holding_sessions": 1},
            {"strategy": "B", "weight": "0.22", "priority": 2, "holding_sessions": 2},
        ],
        "sessions": frames,
        "limits": limits,
    }


@pytest.mark.parametrize(
    "direction,final,a_net,b_net", [(1, "99991", "-4", "-5"), (-1, "99936", "-15", "-49")]
)
def test_independent_multi_horizon_cash_and_fee_anchor(direction, final, a_net, b_net):
    result = run_portfolio_simulation(accounting_source(direction))
    assert result["status"] == "complete"
    assert Decimal(result["metrics"]["final_equity"]) == Decimal(final)
    assert Decimal(result["metrics"]["final_cash"]) == Decimal(final)
    assert result["metrics"]["accounting_reconciled"]
    assert result["metrics"]["all_positions_flat"]
    assert Decimal(result["per_strategy"]["A"]["total_net_pnl"]) == Decimal(a_net)
    assert Decimal(result["per_strategy"]["B"]["total_net_pnl"]) == Decimal(b_net)
    assert Decimal(result["costs"]["commission"]) == 9
    assert [trade["holding_sessions"] for trade in result["trades"]] == [1, 2]
    assert all(order["requested_at"] < order["execution_at"] for order in result["orders"])


def test_real_engine_failure_propagates_without_discarding_other_arms():
    source = accounting_source(1)
    late = (
        datetime.fromisoformat(source["sessions"][1]["decision_at"]) + timedelta(seconds=1)
    ).isoformat()
    source["market"]["sessions"][1]["bars"][0]["known_at"] = late
    source["sessions"][1]["decision_instruments"][0]["mark_known_at"] = late
    result = compare_portfolio_strategies(source)
    assert result["status"] == "blocked"
    assert not result["comparison_complete"]
    assert result["combined"]["reason"] == "held_position_close_unavailable"
    assert result["combined"]["positions"]
    assert set(result["isolated"]) == {"A", "B"}
    assert len(result["blocked_arms"]) == 3
