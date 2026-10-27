"""A fully declared synthetic account with overlapping strategy contributions."""

from trading_system.portfolio.models import PlanInput
from trading_system.storage.store import digest


def synthetic_plan() -> PlanInput:
    decision = "2020-01-06T07:00:00+00:00"
    instruments = []
    for code, sector, price in (("SYN_A", "sector_a", "100"), ("SYN_B", "sector_b", "200")):
        instruments.append(
            {
                "code": code,
                "sector": sector,
                "mark_price": price,
                "mark_as_of": decision,
                "mark_known_at": decision,
                "planning_price": price,
                "rule_known_at": decision,
                "valid_from": "2020-01-06T00:00:00+00:00",
                "valid_until": "2020-01-07T00:00:00+00:00",
                "lot_size": 100,
                "short_allowed": True,
                "short_total_capacity": 10000,
                "short_additional_collateral_ratio": "0.5",
                "fee_fixed": "10",
                "fee_per_share": "0.1",
                "adverse_move_fraction": "0.1",
            }
        )
    signals = []
    for strategy, code, direction in (
        ("short_term", "SYN_A", 1),
        ("swing", "SYN_A", -1),
        ("medium", "SYN_B", -1),
    ):
        signals.append(
            {
                "strategy": strategy,
                "code": code,
                "direction": direction,
                "decision_at": decision,
                "known_at": decision,
                "feature_known_at": decision,
                "trained_at": "2020-01-03T00:00:00+00:00",
                "model_id": "declared_synthetic_model",
                "source_result_hash": digest(
                    b"constructed_forecasts_not_real_research_performance"
                ),
                "confidence": "0.5",
            }
        )
    return PlanInput.model_validate(
        {
            "mode": "synthetic",
            "source_label": "constructed_allocation_fixture_not_market_evidence",
            "decision_at": decision,
            "account": {
                "cash": "100000",
                "as_of": decision,
                "known_at": decision,
                "day_start_equity": "100000",
                "reference_equity": "100000",
                "source_id": "synthetic_account_snapshot",
            },
            "instruments": instruments,
            "strategies": [
                {"strategy": "short_term", "weight": "0.3", "priority": 1},
                {"strategy": "swing", "weight": "0.1", "priority": 2},
                {"strategy": "medium", "weight": "0.2", "priority": 3},
            ],
            "positions": [],
            "reservations": [],
            "signals": signals,
            "correlations": [
                {
                    "code_a": "SYN_A",
                    "code_b": "SYN_B",
                    "correlation": "0.2",
                    "window_end": decision,
                    "known_at": decision,
                    "valid_until": "2020-01-07T00:00:00+00:00",
                }
            ],
            "limits": {
                "max_symbol_fraction": "0.4",
                "max_sector_fraction": "0.5",
                "max_long_fraction": "0.6",
                "max_short_fraction": "0.4",
                "max_gross_fraction": "1",
                "short_credit_limit": "50000",
                "min_cash_ratio": "0.2",
                "max_daily_loss_fraction": "0.05",
                "max_reference_loss_fraction": "0.2",
                "max_stress_loss_fraction": "0.2",
                "correlation_threshold": "0.8",
                "correlated_pair_cap": "0.5",
                "max_evidence_age_seconds": 3600,
            },
        }
    )
