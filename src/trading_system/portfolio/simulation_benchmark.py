"""Dated synthetic portfolio signals, planning snapshots and opening market truth."""

from datetime import datetime, timedelta
from decimal import Decimal

from trading_system.backtest.benchmark import synthetic_scenario
from trading_system.portfolio.benchmark import synthetic_plan
from trading_system.portfolio.simulation_models import SimulationInput
from trading_system.storage.store import digest


def synthetic_simulation() -> SimulationInput:
    market = synthetic_scenario().model_dump(mode="json")
    market["source_label"] = "constructed_portfolio_market_no_real_investment_validity"
    for session in market["sessions"]:
        for rule in session["rules"]:
            rule["commission_rate"] = "0.0005"
            rule["minimum_commission"] = "1"
            rule["slippage_ticks"] = 0
            rule["borrow_annual_rate"] = "0.0365"
    template = synthetic_plan().model_dump(mode="json")
    frames = []
    source_hash = digest(b"constructed_multistrategy_forecasts_not_actual_model_performance")
    for session in market["sessions"]:
        opening, decision = session["open_at"], session["decision_at"]
        future = (datetime.fromisoformat(decision) + timedelta(days=2)).isoformat()
        opening_instruments, decision_instruments = [], []
        for bar, rule in zip(session["bars"], session["rules"], strict=True):
            common = {
                "code": bar["code"],
                "sector": bar["code"] + "_sector",
                "lot_size": rule["lot_size"],
                "short_allowed": rule["short_allowed"],
                "short_total_capacity": rule["short_inventory"],
                "short_additional_collateral_ratio": rule["margin_ratio"],
                "fee_fixed": "10",
                "fee_per_share": "0.1",
                "adverse_move_fraction": "0.1",
                "valid_from": opening,
                "valid_until": future,
            }
            opening_instruments.append(
                common
                | {
                    "mark_price": bar["open"],
                    "mark_as_of": opening,
                    "mark_known_at": opening,
                    "planning_price": bar["open"],
                    "rule_known_at": opening,
                }
            )
            decision_instruments.append(
                common
                | {
                    "mark_price": bar["close"],
                    "mark_as_of": bar["close_at"],
                    "mark_known_at": bar["known_at"],
                    "planning_price": str(Decimal(bar["close"]) * Decimal("1.05")),
                    "rule_known_at": decision,
                }
            )

        def pair(at):
            return [
                {
                    "code_a": "SYN_LONG",
                    "code_b": "SYN_SHORT",
                    "correlation": "0.2",
                    "window_end": at,
                    "known_at": at,
                    "valid_until": future,
                }
            ]

        signals = [
            {
                "strategy": strategy,
                "code": code,
                "direction": direction,
                "decision_at": decision,
                "known_at": decision,
                "feature_known_at": decision,
                "trained_at": opening,
                "model_id": "synthetic_frozen_forecast",
                "source_result_hash": source_hash,
            }
            for strategy, code, direction in (
                ("short", "SYN_LONG", 1),
                ("swing", "SYN_LONG", 1),
                ("medium", "SYN_SHORT", -1),
            )
        ]
        frames.append(
            {
                "decision_at": decision,
                "opening_instruments": opening_instruments,
                "opening_correlations": pair(opening),
                "decision_instruments": decision_instruments,
                "decision_correlations": pair(decision),
                "signals": signals,
            }
        )
    limits = template["limits"] | {
        "max_symbol_fraction": "0.6",
        "max_sector_fraction": "0.6",
        "max_short_fraction": "0.5",
        "short_credit_limit": "60000",
        "min_cash_ratio": "0.1",
        "max_daily_loss_fraction": "0.2",
    }
    return SimulationInput.model_validate(
        {
            "mode": "synthetic",
            "source_label": "constructed_fixed_portfolio_replay_not_market_evidence",
            "market": market,
            "strategies": [
                {"strategy": "short", "weight": "0.25", "priority": 1, "holding_sessions": 1},
                {"strategy": "swing", "weight": "0.15", "priority": 2, "holding_sessions": 3},
                {"strategy": "medium", "weight": "0.2", "priority": 3, "holding_sessions": 2},
            ],
            "sessions": frames,
            "limits": limits,
        }
    )
