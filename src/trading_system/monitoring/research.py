"""Observe saved synthetic research cohorts; never fit, infer, select or deploy."""

import hashlib
import math
import re
from datetime import datetime

import numpy as np

from trading_system.backtest.models import Scenario
from trading_system.point_in_time.policy import aware_utc
from trading_system.research.features import FEATURES, HORIZONS
from trading_system.research.features import VERSION as FEATURE_VERSION
from trading_system.storage.store import canonical

PIPELINE_VERSION = "synthetic_causal_model_research_v1"
RTOL, ATOL = 1e-10, 1e-12


def _require(value):
    if not value:
        raise ValueError("Invalid saved research evidence")


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _verify_hash(value, key):
    _require(
        type(value) is dict
        and value.get(key) == _hash({k: v for k, v in value.items() if k != key})
    )


def _number(value):
    _require(type(value) in {int, float} and math.isfinite(value))
    return float(value)


def _time(value):
    _require(isinstance(value, str))
    return aware_utc(datetime.fromisoformat(value))


def _key(value):
    _require(type(value["decision_index"]) is int and value["decision_index"] >= 0)
    _require(isinstance(value["code"], str) and bool(value["code"]))
    return value["decision_index"], value["code"]


def _candidate(value):
    _require(type(value) is dict and set(value) == {"name", "kind", "alpha"})
    _require(isinstance(value["name"], str) and bool(value["name"]))
    _require(value["kind"] in {"zero", "mean", "ridge", "lightgbm"})
    _require(_number(value["alpha"]) > 0 if value["kind"] == "ridge" else value["alpha"] is None)


def _table(table, horizon, scenario, input_hash):
    _require(table["mode"] == "synthetic" and table["version"] == FEATURE_VERSION)
    _require(type(table["horizon_sessions"]) is int and table["horizon_sessions"] == horizon)
    _require(table["input_hash"] == input_hash and table["feature_names"] == list(FEATURES))
    _require(table["target"] == "raw_open[t+h+1]/raw_open[t+1]-1")
    _require(
        table["benchmark_features_used"] is False and table["corporate_actions_supported"] is False
    )
    rows = {}
    bars = [{bar.code: bar for bar in day.bars} for day in scenario.sessions]
    expected_keys = set()
    for t, day in enumerate(scenario.sessions):
        if t < 20 or t + horizon + 1 >= len(bars):
            continue
        for code in day.universe:
            if (
                all(bars[index][code].known_at <= day.decision_at for index in range(t - 20, t + 1))
                and bars[t + 1][code].open is not None
                and bars[t + horizon + 1][code].open is not None
            ):
                expected_keys.add((t, code))
    for row in table["samples"]:
        key = _key(row)
        t, code = key
        _require(key not in rows and 20 <= t < len(scenario.sessions))
        _require(code in scenario.sessions[t].universe)
        _require(_time(row["decision_at"]) == scenario.sessions[t].decision_at)
        _require(len(row["features"]) == len(FEATURES))
        [_number(value) for value in row["features"]]
        _number(row["label"])
        _require(type(row["entry_index"]) is int and row["entry_index"] == t + 1)
        _require(
            type(row["label_end_index"]) is int
            and row["label_end_index"] == t + horizon + 1 < len(bars)
        )
        _require(
            bars[t + 1][code].open is not None and bars[t + horizon + 1][code].open is not None
        )
        expected_label = float(bars[t + horizon + 1][code].open / bars[t + 1][code].open - 1)
        _require(np.isclose(row["label"], expected_label, rtol=RTOL, atol=ATOL))
        _require(
            _time(row["label_known_at"])
            == max(bars[t + 1][code].known_at, bars[t + horizon + 1][code].known_at)
        )
        sources = row["feature_sources"]
        _require(row["feature_source_hash"] == _hash(sources) and len(sources) == 21)
        for index, source in zip(range(t - 20, t + 1), sources, strict=True):
            bar = bars[index][code]
            _require(type(source["session_index"]) is int and source["session_index"] == index)
            _require(
                source["close"] == str(bar.close) and _time(source["known_at"]) == bar.known_at
            )
        _require(
            _time(row["feature_known_at"])
            == max(_time(item["known_at"]) for item in sources)
            <= _time(row["decision_at"])
        )
        rows[key] = row
    _require(bool(rows) and set(rows) == expected_keys)
    return rows


def _model(model, rows, names, input_hash, boundary, cutoff):
    _verify_hash(model, "model_id")
    _require(model["format"] == "safe_json_native_model_v1" and model["mode"] == "synthetic")
    _require(model["deployment_enabled"] is False and model["input_hash"] == input_hash)
    _require(model["feature_names"] == names and _time(model["trained_at"]) == cutoff)
    _candidate(model["candidate"])
    _number(model["intercept"])
    _require(
        len(model["scaler_mean"])
        == len(model["scaler_scale"])
        == len(model["constant_columns"])
        == len(names)
    )
    [_number(value) for value in model["scaler_mean"]]
    _require(all(_number(value) > 0 for value in model["scaler_scale"]))
    _require(all(type(value) is bool for value in model["constant_columns"]))
    kind = model["candidate"]["kind"]
    if kind == "ridge":
        _require(len(model["coefficients"]) == len(names))
        [_number(value) for value in model["coefficients"]]
    else:
        _require(model["coefficients"] is None)
    _require(
        isinstance(model["native_model"], str) and bool(model["native_model"])
        if kind == "lightgbm"
        else model["native_model"] is None
    )
    _require(type(model["parameters"]) is dict and type(model["library_versions"]) is dict)
    selected = []
    keys = set()
    for index in model["training_indices"]:
        key = _key(index)
        _require(key not in keys and key in rows)
        keys.add(key)
        row = rows[key]
        _require(
            index
            == {
                field: row[field]
                for field in (
                    "decision_index",
                    "code",
                    "label_end_index",
                    "label_known_at",
                    "feature_known_at",
                )
            }
        )
        _require(row["label_end_index"] < boundary and row["decision_index"] < boundary)
        _require(_time(row["label_known_at"]) <= cutoff)
        selected.append(row)
    eligible = {
        key
        for key, row in rows.items()
        if row["decision_index"] < boundary
        and row["label_end_index"] < boundary
        and _time(row["label_known_at"]) <= cutoff
    }
    _require(keys == eligible and bool(keys))
    return selected


def _predictions(predictions, cohort, model):
    _require(len(predictions) == len(cohort))
    values, keys = [], set()
    for prediction, row in zip(predictions, cohort, strict=True):
        key = _key(prediction)
        _require(key == _key(row) and key not in keys)
        keys.add(key)
        _require(prediction["model_id"] == model["model_id"])
        _require(
            _time(prediction["decision_at"])
            == _time(row["decision_at"])
            == _time(prediction["known_at"])
        )
        _require(_time(prediction["feature_known_at"]) == _time(row["feature_known_at"]))
        _require(
            _time(prediction["trained_at"])
            == _time(model["trained_at"])
            <= _time(prediction["known_at"])
        )
        values.append(_number(prediction["prediction"]))
    return np.asarray(values)


def _score(predictions, cohort, saved):
    _require(type(saved) is dict and set(saved) == {"mse", "mae", "direction_accuracy", "rows"})
    _require(type(saved["rows"]) is int and saved["rows"] == len(cohort))
    for key in ("mse", "mae"):
        _require(_number(saved[key]) >= 0)
    _require(0 <= _number(saved["direction_accuracy"]) <= 1)
    labels = np.asarray([_number(row["label"]) for row in cohort])
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        score = {
            "mse": float(np.mean((predictions - labels) ** 2)),
            "mae": float(np.mean(np.abs(predictions - labels))),
            "direction_accuracy": float(np.mean(np.sign(predictions) == np.sign(labels))),
            "rows": len(cohort),
        }
    for key in ("mse", "mae", "direction_accuracy"):
        _require(
            math.isfinite(score[key])
            and np.isclose(score[key], _number(saved[key]), rtol=RTOL, atol=ATOL)
        )
    return score


def _shift(reference, evaluation, threshold):
    train = np.asarray([row["features"] for row in reference], dtype=float)
    final = np.asarray([row["features"] for row in evaluation], dtype=float)
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        mean, current, std = train.mean(axis=0), final.mean(axis=0), train.std(axis=0, ddof=0)
        constant_reference = np.all(train == train[0], axis=0)
        constant_final = np.all(final == final[0], axis=0)
        mean[constant_reference], std[constant_reference] = train[0, constant_reference], 0
        current[constant_final] = final[0, constant_final]
    _require(np.isfinite(mean).all() and np.isfinite(current).all() and np.isfinite(std).all())
    reports = []
    for name, old, new, scale in zip(FEATURES, mean, current, std, strict=True):
        value = float(abs(new - old) / scale) if scale else 0.0 if old == new else None
        _require(value is None or math.isfinite(value))
        reports.append(
            {
                "feature": name,
                "reference_mean": float(old),
                "evaluation_mean": float(new),
                "reference_population_std": float(scale),
                "standardized_mean_shift": value,
                "status": "zero_variance_mean_changed"
                if value is None
                else "threshold_exceeded"
                if value > threshold
                else "within_declared_threshold",
            }
        )
    return reports


def monitor_research(
    result: dict,
    *,
    now: datetime,
    max_mse_ratio: float,
    max_mean_shift: float,
    minimum_rows: int = 20,
) -> dict:
    """Validate saved cohorts and observe heuristics, without creating new trials."""
    try:
        now = aware_utc(now)
        _require(_number(max_mse_ratio) > 0 and _number(max_mean_shift) > 0)
        _require(type(minimum_rows) is int and minimum_rows >= 2)
        _verify_hash(result, "result_hash")
        _require(
            result["status"] == "complete"
            and result["mode"] == "synthetic"
            and result["pipeline_version"] == PIPELINE_VERSION
        )
        _require(
            isinstance(result["experiment_id"], str)
            and re.fullmatch(r"[a-f0-9]{64}", result["experiment_id"])
        )
        _require(
            result["assumptions"]["deployment_enabled"] is False
            and result["assumptions"]["historical_market_validity"] is False
        )
        scenario = Scenario.model_validate(result["scenario"])
        _require(
            not scenario.actions and scenario.signal_policy == "momentum" and not scenario.signals
        )
        _require(result["input_hash"] == _hash(scenario.model_dump(mode="json")))
        config = result["configuration"]
        for field in ("development_start", "final_start", "final_end"):
            _require(type(config[field]) is int)
        _require(20 < config["development_start"] < config["final_start"] < config["final_end"])
        _require(config["final_end"] + max(HORIZONS) < len(scenario.sessions))
        horizons = {str(horizon) for horizon in HORIZONS}
        _require(
            set(result["feature_label_tables"])
            == set(result["final_results"])
            == set(result["selection"])
            == horizons
        )
        reports, alerts = {}, []
        for horizon in HORIZONS:
            key = str(horizon)
            rows = _table(
                result["feature_label_tables"][key], horizon, scenario, result["input_hash"]
            )
            final = result["final_results"][key]
            selected, baseline = final["selected_model"], final["baseline_model"]
            cutoff = scenario.sessions[config["final_start"]].open_at
            reference = _model(
                selected, rows, list(FEATURES), result["input_hash"], config["final_start"], cutoff
            )
            baseline_reference = _model(
                baseline, rows, list(FEATURES), result["input_hash"], config["final_start"], cutoff
            )
            _require([_key(row) for row in reference] == [_key(row) for row in baseline_reference])
            _require(
                selected["candidate"] == result["selection"][key]["selected"]
                and baseline["candidate"] == result["selection"][key]["baseline"]
            )
            _require(baseline["candidate"]["kind"] in {"zero", "mean"})
            evaluation = []
            keys = set()
            for index in final["evaluation_indices"]:
                _require(set(index) == {"decision_index", "code"})
                item = _key(index)
                _require(item in rows and item not in keys)
                keys.add(item)
                evaluation.append(rows[item])
            _require(
                keys
                == {
                    item
                    for item, row in rows.items()
                    if config["final_start"] <= row["decision_index"] < config["final_end"]
                }
                and bool(keys)
            )
            predictions = _predictions(final["predictions"], evaluation, selected)
            baseline_predictions = _predictions(final["baseline_predictions"], evaluation, baseline)
            evaluation_at = _time(final["evaluation_at"])
            _require(evaluation_at == scenario.sessions[-1].decision_at)
            _require(evaluation_at >= max(_time(row["label_known_at"]) for row in evaluation))
            available_at = max(
                evaluation_at, *(_time(row["label_known_at"]) for row in evaluation + reference)
            )
            reasons = []
            if now < available_at:
                reasons.append("required_outcomes_not_yet_known")
            if min(len(reference), len(evaluation)) < minimum_rows:
                reasons.append("insufficient_cohort_rows")
            _require(type(final["cost_evaluation_complete"]) is bool)
            if not final["cost_evaluation_complete"]:
                reasons.append("historical_cost_evaluation_incomplete")
            report = {
                "horizon_sessions": horizon,
                "selected_model_id": selected["model_id"],
                "baseline_model_id": baseline["model_id"],
                "reference_rows": len(reference),
                "evaluation_rows": len(evaluation),
                "evaluation_at": evaluation_at.isoformat(),
                "required_outcomes_known_at": available_at.isoformat(),
                "metrics_available": False,
                "selected_score": None,
                "baseline_score": None,
                "mse_ratio": None,
                "mse_ratio_status": "unavailable",
                "feature_mean_shift": None,
                "historical_cost_evaluation_complete": final["cost_evaluation_complete"],
            }
            if now >= available_at and min(len(reference), len(evaluation)) >= minimum_rows:
                score = _score(predictions, evaluation, final["selected_score"])
                baseline_score = _score(baseline_predictions, evaluation, final["baseline_score"])
                ratio = (
                    score["mse"] / baseline_score["mse"]
                    if baseline_score["mse"]
                    else 1.0
                    if score["mse"] == 0
                    else None
                )
                _require(ratio is None or math.isfinite(ratio))
                shifts = _shift(reference, evaluation, max_mean_shift)
                if ratio is None:
                    reasons.append("baseline_zero_selected_nonzero")
                elif ratio > max_mse_ratio:
                    reasons.append("mse_ratio_threshold_exceeded")
                if any(item["status"] != "within_declared_threshold" for item in shifts):
                    reasons.append("feature_mean_shift_attention")
                report.update(
                    metrics_available=True,
                    selected_score=score,
                    baseline_score=baseline_score,
                    mse_ratio=ratio,
                    mse_ratio_status="both_zero_equal_ratio_one"
                    if score["mse"] == baseline_score["mse"] == 0
                    else "baseline_zero_selected_nonzero"
                    if ratio is None
                    else "available",
                    feature_mean_shift=shifts,
                )
            report.update(
                status="attention" if reasons else "observed",
                alerts=[{"code": reason} for reason in reasons],
            )
            reports[key] = report
            alerts.extend({"horizon_sessions": horizon, "code": reason} for reason in reasons)
        _require(
            type(result["cost_evaluation_complete"]) is bool
            and result["cost_evaluation_complete"]
            == all(report["historical_cost_evaluation_complete"] for report in reports.values())
        )
        output = {
            "schema_version": 1,
            "status": "attention" if alerts else "observed",
            "mode": "saved_synthetic_research",
            "read_only": True,
            "observed_at": now.isoformat(),
            "deployment_enabled": False,
            "real_execution_enabled": False,
            "historical_market_validity": False,
            "current_model_health_verified": False,
            "trade_permission": None,
            "retraining_performed": False,
            "policy": {
                "max_mse_ratio": float(max_mse_ratio),
                "max_mean_shift": float(max_mean_shift),
                "minimum_rows": minimum_rows,
                "reference": "frozen_selected_model_training_indices",
                "evaluation": "saved_final_evaluation_indices_no_runtime_filtering",
                "mean_shift": "absolute_mean_difference_over_reference_population_std",
                "limitations": "mean_only_heuristic_not_full_distribution_drift_or_significance",
                "score_tolerance": {"rtol": RTOL, "atol": ATOL},
                "state_semantics": "saved_synthetic_evaluation_not_live_or_historical_pit",
            },
            "horizons": reports,
            "alerts": alerts,
            "source": {
                "result_hash": result["result_hash"],
                "input_hash": result["input_hash"],
                "experiment_id": result["experiment_id"],
                "pipeline_version": PIPELINE_VERSION,
            },
        }
        output["result_hash"] = _hash(output)
        return output
    except Exception:
        raise ValueError("Research monitoring source unavailable or invalid") from None
