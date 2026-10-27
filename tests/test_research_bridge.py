from copy import deepcopy

import pytest

from trading_system.backtest.benchmark import synthetic_scenario
from trading_system.research.bridge import compare_predictions


def predictions(scenario):
    return [
        {
            "decision_index": 1,
            "code": code,
            "prediction": value,
            "decision_at": scenario.sessions[1].decision_at.isoformat(),
            "feature_known_at": scenario.sessions[1].bars[0].known_at.isoformat(),
            "trained_at": scenario.sessions[0].decision_at.isoformat(),
            "model_id": "test_frozen_model",
        }
        for code, value in (("SYN_LONG", 0.1), ("SYN_SHORT", -0.1))
    ]


def test_forecast_and_momentum_have_identical_cost_and_execution_conditions():
    scenario = synthetic_scenario()
    before = scenario.model_dump(mode="json")
    result = compare_predictions(scenario, 5, predictions(scenario), 1, 8)
    model, baseline = result["prediction"], result["momentum"]
    assert model["status"] == baseline["status"] == "complete"
    assert model["orders"] == baseline["orders"]
    assert model["trades"] == baseline["trades"]
    assert len(model["trades"]) == 2
    assert all(trade["holding_sessions"] == 5 for trade in model["trades"])
    assert model["metrics"]["costs"]["commission"] != "0"
    assert model["metrics"]["costs"]["borrow"] != "0"
    assert not model["positions"]
    assert model["orders"][0]["execution_at"] == scenario.sessions[2].open_at.isoformat()
    assert not result["historical_market_validity"]
    assert scenario.model_dump(mode="json") == before


def test_zero_forecast_means_cash_without_suppressing_fair_baseline():
    scenario = synthetic_scenario()
    records = predictions(scenario)
    for row in records:
        row["prediction"] = 0.0
    result = compare_predictions(scenario, 5, records, 1, 8)
    assert result["prediction"]["orders"] == []
    assert len(result["momentum"]["trades"]) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"prediction": float("nan")},
        {"prediction": float("inf")},
        {"prediction": True},
        {"decision_index": 2},
        {"code": "not-in-universe"},
        {"decision_at": "2020-01-07T06:32:00+00:00"},
        {"trained_at": "2020-01-09T06:30:00+00:00"},
        {"feature_known_at": "2020-01-09T06:30:00+00:00"},
    ],
)
def test_invalid_or_leaky_prediction_is_rejected(change):
    scenario = synthetic_scenario()
    records = predictions(scenario)
    records[0].update(change)
    with pytest.raises(ValueError):
        compare_predictions(scenario, 5, records, 1, 8)


def test_duplicate_and_short_execution_tail_rejected():
    scenario = synthetic_scenario()
    records = predictions(scenario)
    with pytest.raises(ValueError, match="Duplicate"):
        compare_predictions(scenario, 5, records + [records[0]], 1, 8)
    with pytest.raises(ValueError, match="tail"):
        compare_predictions(scenario, 5, records, 1, 7)


def test_supplied_forecasts_still_respect_short_inventory():
    scenario = synthetic_scenario().model_dump(mode="json")
    records = predictions(synthetic_scenario())
    scenario["sessions"][2]["rules"][1]["short_inventory"] = 0
    result = compare_predictions(scenario, 5, records, 1, 8)
    for arm in ("prediction", "momentum"):
        rejected = [order for order in result[arm]["orders"] if order["status"] == "rejected"]
        assert any(order["reason"] == "short_inventory_insufficient" for order in rejected)


def test_future_price_changes_do_not_change_earlier_requests():
    scenario = synthetic_scenario().model_dump(mode="json")
    records = predictions(synthetic_scenario())
    changed = deepcopy(scenario)
    changed["sessions"][6]["bars"][0]["close"] = "107"
    first = compare_predictions(scenario, 5, records, 1, 8)
    second = compare_predictions(changed, 5, records, 1, 8)
    for arm in ("prediction", "momentum"):

        def requests(result):
            return [
                (o["code"], o["side"], o["quantity"], o["requested_at"])
                for o in result[arm]["orders"]
            ]

        assert requests(first) == requests(second)
