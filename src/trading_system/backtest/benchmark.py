"""Small fixed synthetic experiment, with every fee and execution rule supplied."""

from datetime import date, datetime, time, timedelta, timezone

from trading_system.backtest.models import Scenario


def synthetic_scenario() -> Scenario:
    sessions = []
    first = date(2020, 1, 6)
    long_closes = [100, 101, 103, 102, 104, 105, 103, 106]
    short_closes = [100, 99, 97, 98, 96, 95, 97, 94]
    for index, day_offset in enumerate([0, 1, 2, 3, 4, 7, 8, 9]):
        day = first + timedelta(days=day_offset)
        opening = datetime.combine(day, time(0), timezone.utc)
        close = opening + timedelta(hours=6, minutes=30)
        bars, rules = [], []
        for code, values in (("SYN_LONG", long_closes), ("SYN_SHORT", short_closes)):
            bars.append(
                {
                    "code": code,
                    "open": str(values[max(index - 1, 0)]),
                    "close": str(values[index]),
                    "close_at": close,
                    "known_at": close,
                }
            )
            rules.append(
                {
                    "code": code,
                    "known_at": opening,
                    "lot_size": 100,
                    "tick_size": "1",
                    "limit_lower": "10",
                    "limit_upper": "1000",
                    "opening_executable_quantity": 10000,
                    "can_buy": True,
                    "can_sell": True,
                    "short_allowed": True,
                    "short_inventory": 10000,
                    "commission_rate": "0.0005",
                    "minimum_commission": "1",
                    "slippage_ticks": 1,
                    "borrow_annual_rate": "0.01",
                    "borrow_day_count": 365,
                    "reverse_fee_per_share_per_day": "0",
                    "margin_ratio": "1",
                }
            )
        sessions.append(
            {
                "date": day,
                "open_at": opening,
                "decision_at": close + timedelta(minutes=1),
                "bars": bars,
                "rules": rules,
                "universe": ["SYN_LONG", "SYN_SHORT"],
                "universe_known_at": opening,
                "benchmark_close": str(1000 + index),
            }
        )
    return Scenario.model_validate(
        {
            "mode": "synthetic",
            "source_label": "constructed_example_not_market_evidence",
            "declared_codes": ["SYN_LONG", "SYN_SHORT"],
            "initial_cash": "100000",
            "strategy": {"quantity": 100, "holding_sessions": 2, "max_allocation_fraction": "0.25"},
            "risk_free_annual_rate": "0",
            "sessions_per_year": 252,
            "sessions": sessions,
            "actions": [],
        }
    )
