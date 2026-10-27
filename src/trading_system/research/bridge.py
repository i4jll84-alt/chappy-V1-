"""Compare forecasts and a fixed momentum rule through the same synthetic broker."""

import math
from datetime import datetime

from trading_system.backtest.engine import run_backtest
from trading_system.backtest.models import Scenario
from trading_system.point_in_time.policy import aware_utc


def compare_predictions(
    scenario: Scenario | dict,
    horizon: int,
    predictions: list[dict],
    start_index: int,
    end_index: int,
    threshold: float = 0.0,
) -> dict:
    """Use identical decision keys, execution tail, capital and costs for both arms.

    The window is half-open in original session indexes. Its tail includes every
    requested horizon's exit session. Neither arm can enter during a tail without
    a supplied eligible prediction. Each starts with cash and no positions.
    """
    scenario = Scenario.model_validate(scenario)
    if scenario.actions:
        raise ValueError("Research bridge requires an action-free synthetic scenario")
    if type(horizon) is not int or horizon not in (5, 10, 20, 60):
        raise ValueError("Unsupported research horizon")
    if (
        type(start_index) is not int
        or type(end_index) is not int
        or not 1 <= start_index < end_index <= len(scenario.sessions)
        or end_index - start_index < 2
    ):
        raise ValueError("Invalid evaluation window")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or threshold < 0
    ):
        raise ValueError("Threshold must be finite and nonnegative")
    if not predictions:
        raise ValueError("No eligible out-of-sample predictions")
    forecast_signals, momentum_signals, seen = [], [], set()
    for record in sorted(predictions, key=lambda r: (r["decision_index"], r["code"])):
        index, code = record["decision_index"], record["code"]
        if type(index) is not int or not start_index <= index < end_index:
            raise ValueError("Prediction outside the evaluation window")
        if index + 1 + horizon >= end_index:
            raise ValueError("Prediction horizon exceeds the execution tail")
        if (index, code) in seen:
            raise ValueError("Duplicate prediction key")
        seen.add((index, code))
        value = record["prediction"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("Prediction must be finite")
        session = scenario.sessions[index]
        decision = aware_utc(datetime.fromisoformat(record["decision_at"]))
        if decision != session.decision_at or code not in session.universe:
            raise ValueError("Prediction does not match the dated decision universe")
        known = aware_utc(datetime.fromisoformat(record.get("known_at", record["decision_at"])))
        common = {
            "code": code,
            "decision_at": decision,
            "known_at": known,
            "trained_at": aware_utc(datetime.fromisoformat(record["trained_at"])),
        }
        forecast_signals.append(
            common
            | {
                "feature_cutoff_at": aware_utc(datetime.fromisoformat(record["feature_known_at"])),
                "direction": 1 if value > threshold else (-1 if value < -threshold else 0),
                "model_id": record["model_id"],
            }
        )
        current = next(bar for bar in session.bars if bar.code == code)
        previous = next(bar for bar in scenario.sessions[index - 1].bars if bar.code == code)
        cutoff = max(current.known_at, previous.known_at)
        if cutoff > decision:
            raise ValueError("Momentum benchmark prices unavailable at the decision")
        direction = (
            1 if current.close > previous.close else (-1 if current.close < previous.close else 0)
        )
        momentum_signals.append(
            common
            | {
                "known_at": decision,
                "feature_cutoff_at": cutoff,
                "direction": direction,
                "model_id": "fixed_one_session_momentum_v1",
            }
        )

    base = scenario.model_dump(mode="json")
    base["sessions"] = base["sessions"][start_index:end_index]
    base["strategy"]["holding_sessions"] = horizon
    base["signal_policy"] = "supplied"
    results = {}
    for name, signals in (("prediction", forecast_signals), ("momentum", momentum_signals)):
        results[name] = run_backtest(base | {"signals": signals})
    return {
        "comparison_policy": "same_decision_keys_flat_start_same_costs_v1",
        "start_index": start_index,
        "end_index": end_index,
        "horizon": horizon,
        "threshold": threshold,
        "eligible_signal_count": len(seen),
        "historical_market_validity": False,
        **results,
    }
