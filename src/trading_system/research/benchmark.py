"""Fixed synthetic market paths for engineering reproducibility, not investment evidence."""

import copy
from datetime import date, datetime, time, timedelta, timezone

import numpy as np

from trading_system.backtest.benchmark import synthetic_scenario
from trading_system.backtest.models import Scenario


def model_scenario() -> Scenario:
    source = synthetic_scenario().model_dump(mode="json")
    template = source["sessions"][0]
    source["sessions"] = []
    source["initial_cash"] = "1000000"
    source["strategy"]["max_allocation_fraction"] = "0.1"
    source["source_label"] = "seed_1729_constructed_model_research_not_market_data"
    rng = np.random.default_rng(1729)
    prices, previous_returns = (
        {"SYN_LONG": 100.0, "SYN_SHORT": 130.0},
        dict.fromkeys(source["declared_codes"], 0.0),
    )
    day = date(2020, 1, 6)
    for index in range(420):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        opening = datetime.combine(day, time(0), timezone.utc)
        close = opening + timedelta(hours=6, minutes=30)
        session = copy.deepcopy(template)
        session.update(
            date=day.isoformat(),
            open_at=opening.isoformat(),
            decision_at=(close + timedelta(minutes=1)).isoformat(),
            universe_known_at=opening.isoformat(),
            benchmark_close=str(1000 + index),
        )
        for bar, rule in zip(session["bars"], session["rules"], strict=True):
            code = bar["code"]
            raw_open = prices[code] * (1 + rng.normal(0, 0.0005))
            regime = 0.0005 if (index // 70) % 2 == 0 else -0.0005
            daily_return = regime + 0.6 * previous_returns[code] + rng.normal(0, 0.002)
            prices[code] *= 1 + daily_return
            previous_returns[code] = daily_return
            bar.update(
                open=f"{raw_open:.8f}",
                close=f"{prices[code]:.8f}",
                close_at=close.isoformat(),
                known_at=close.isoformat(),
            )
            rule.update(
                known_at=opening.isoformat(),
                tick_size="0.01",
                limit_lower="1",
                limit_upper="1000000",
                slippage_ticks=1,
            )
        source["sessions"].append(session)
        day += timedelta(days=1)
    return Scenario.model_validate(source)
