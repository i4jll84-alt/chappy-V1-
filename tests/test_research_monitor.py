"""Observe frozen synthetic cohorts without training, inference or final reruns."""

import copy
import hashlib
from datetime import datetime, timezone

import numpy as np
import pytest

from trading_system.monitoring.research import _shift, monitor_research
from trading_system.research.benchmark import model_scenario
from trading_system.research.features import FEATURES, HORIZONS
from trading_system.research.pipeline import run_research
from trading_system.storage.store import canonical

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def rehash(value, key="result_hash"):
    value[key] = hashlib.sha256(
        canonical({k: v for k, v in value.items() if k != key}).encode()
    ).hexdigest()


@pytest.fixture(scope="module")
def saved(tmp_path_factory):
    source = model_scenario().model_dump(mode="json")
    source["declared_codes"] = ["SYN_LONG"]
    source["sessions"] = source["sessions"][:310]
    for session in source["sessions"]:
        session["bars"] = session["bars"][:1]
        session["rules"] = session["rules"][:1]
        session["universe"] = ["SYN_LONG"]
    config = {
        "development_start": 120,
        "final_start": 210,
        "final_end": 240,
        "minimum_train_rows": 10,
        "minimum_evaluation_rows": 3,
        "candidates": [{"name": "zero", "kind": "zero"}, {"name": "mean", "kind": "mean"}],
    }
    return run_research(source, config, tmp_path_factory.mktemp("research-monitor"))


def observe(result, **kwargs):
    return monitor_research(
        result,
        now=kwargs.get("now", NOW),
        max_mse_ratio=kwargs.get("max_mse_ratio", 10),
        max_mean_shift=kwargs.get("max_mean_shift", 100),
        minimum_rows=kwargs.get("minimum_rows", 20),
    )


def test_frozen_result_deterministic_input_unchanged_no_training_inference_or_registry(
    saved, monkeypatch
):
    before = canonical(saved)

    def forbidden(*args, **kwargs):
        raise AssertionError("Monitor must only observe saved results")

    monkeypatch.setattr("trading_system.research.models.train_model", forbidden)
    monkeypatch.setattr("trading_system.research.models.predict_model", forbidden)
    monkeypatch.setattr("trading_system.research.features.build_samples", forbidden)
    monkeypatch.setattr(
        "trading_system.backtest.evaluation.ExperimentRegistry.evaluate_final", forbidden
    )
    result = observe(saved)
    assert result == observe(saved)
    assert canonical(saved) == before
    assert result["status"] == "observed"
    assert set(result["horizons"]) == {str(h) for h in HORIZONS}
    assert result["deployment_enabled"] is False
    assert result["retraining_performed"] is False
    assert result["historical_market_validity"] is False
    assert result["current_model_health_verified"] is False
    assert result["trade_permission"] is None
    for horizon in result["horizons"].values():
        assert horizon["evaluation_rows"] == 30
        assert horizon["metrics_available"] is True
        assert horizon["mse_ratio"] == 1
    assert (
        result["result_hash"]
        == hashlib.sha256(
            canonical({k: v for k, v in result.items() if k != "result_hash"}).encode()
        ).hexdigest()
    )


def test_frozen_exact_training_cohort_mean_std_and_saved_scores(saved):
    result = observe(saved)
    final = saved["final_results"]["60"]
    rows = {
        (r["decision_index"], r["code"]): r for r in saved["feature_label_tables"]["60"]["samples"]
    }
    reference = np.asarray(
        [
            rows[(index["decision_index"], index["code"])]["features"]
            for index in final["selected_model"]["training_indices"]
        ]
    )
    evaluation = np.asarray(
        [
            rows[(index["decision_index"], index["code"])]["features"]
            for index in final["evaluation_indices"]
        ]
    )
    report = result["horizons"]["60"]
    assert report["selected_score"] == final["selected_score"]
    for i, feature in enumerate(report["feature_mean_shift"]):
        assert feature["reference_mean"] == pytest.approx(reference[:, i].mean())
        assert feature["reference_population_std"] == pytest.approx(reference[:, i].std(ddof=0))
        assert feature["standardized_mean_shift"] == pytest.approx(
            abs(evaluation[:, i].mean() - reference[:, i].mean()) / reference[:, i].std(ddof=0)
        )


def test_policy_thresholds_attention_do_not_change_models_or_cohorts(saved):
    report = observe(saved, max_mse_ratio=0.5, max_mean_shift=0.000001)
    assert report["status"] == "attention"
    assert {a["code"] for a in report["alerts"]} >= {
        "mse_ratio_threshold_exceeded",
        "feature_mean_shift_attention",
    }
    for h, item in report["horizons"].items():
        assert item["selected_model_id"] == saved["final_results"][h]["selected_model"]["model_id"]
        assert item["evaluation_rows"] == len(saved["final_results"][h]["evaluation_indices"])


@pytest.mark.parametrize("kind", ["future", "insufficient"])
def test_unavailable_cohort_returns_no_metrics_and_never_filters_rows(saved, monkeypatch, kind):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unavailable cohort must not calculate metrics")

    monkeypatch.setattr("trading_system.monitoring.research._score", forbidden)
    kwargs = (
        {"now": datetime.fromisoformat(saved["scenario"]["sessions"][220]["decision_at"])}
        if kind == "future"
        else {"minimum_rows": 10000}
    )
    report = observe(saved, **kwargs)
    assert report["status"] == "attention"
    for item in report["horizons"].values():
        assert item["evaluation_rows"] == 30
        assert item["metrics_available"] is False
        assert item["selected_score"] is None
        assert item["baseline_score"] is None
        assert item["mse_ratio"] is None
        assert item["feature_mean_shift"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_mse_ratio", 0),
        ("max_mse_ratio", True),
        ("max_mse_ratio", float("inf")),
        ("max_mean_shift", -1),
        ("max_mean_shift", float("nan")),
        ("minimum_rows", True),
        ("minimum_rows", 1),
        ("minimum_rows", 2.5),
    ],
)
def test_strict_monitoring_policy_rejected(saved, field, value):
    with pytest.raises(ValueError, match="^Research monitoring source unavailable or invalid$"):
        observe(saved, **{field: value})


def test_naive_clock_rejected(saved):
    with pytest.raises(ValueError):
        observe(saved, now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "kind",
    [
        "outer_hash",
        "scenario_lineage",
        "nested_hash",
        "feature_order",
        "missing_horizon",
        "model_input_hash",
        "duplicate_eval",
        "missing_eval",
        "missing_training",
        "prediction_key",
        "prediction_model",
        "prediction_time",
        "score",
        "feature_source_hash",
        "label_end",
        "early_eval",
        "experiment_id",
    ],
)
def test_corruption_fails_closed_without_source_details(saved, kind):
    result = copy.deepcopy(saved)
    final, table = result["final_results"]["5"], result["feature_label_tables"]["5"]
    model = final["selected_model"]
    if kind == "outer_hash":
        result["result_hash"] = "a" * 64
    elif kind == "scenario_lineage":
        result["scenario"]["source_label"] = "private_source"
    elif kind == "nested_hash":
        model["intercept"] += 1
    elif kind == "feature_order":
        model["feature_names"].reverse()
        rehash(model, "model_id")
    elif kind == "missing_horizon":
        del result["final_results"]["60"]
    elif kind == "model_input_hash":
        model["input_hash"] = "a" * 64
        rehash(model, "model_id")
    elif kind == "duplicate_eval":
        final["evaluation_indices"][1] = final["evaluation_indices"][0]
    elif kind == "missing_eval":
        final["evaluation_indices"].pop()
        final["predictions"].pop()
        final["baseline_predictions"].pop()
    elif kind == "missing_training":
        model["training_indices"].pop()
        rehash(model, "model_id")
    elif kind == "prediction_key":
        final["predictions"][0]["decision_index"] += 1
    elif kind == "prediction_model":
        final["predictions"][0]["model_id"] = "a" * 64
    elif kind == "prediction_time":
        final["predictions"][0]["trained_at"] = NOW.isoformat()
    elif kind == "score":
        final["selected_score"]["mse"] += 1
    elif kind == "feature_source_hash":
        table["samples"][0]["feature_source_hash"] = "a" * 64
    elif kind == "label_end":
        table["samples"][0]["label_end_index"] -= 1
    elif kind == "early_eval":
        final["evaluation_at"] = result["scenario"]["sessions"][240]["decision_at"]
    elif kind == "experiment_id":
        result["experiment_id"] = "private_source"
    if kind != "outer_hash":
        rehash(result)
    with pytest.raises(ValueError) as caught:
        observe(result)
    assert str(caught.value) == "Research monitoring source unavailable or invalid"
    assert caught.value.__cause__ is None


def test_zero_variance_equal_constant_different_counts_and_changed_mean():
    def cohort(value, size):
        return [{"features": [value] * len(FEATURES)} for _ in range(size)]

    equal = _shift(cohort(0.1, 101), cohort(0.1, 23), 1)
    assert all(
        row["reference_population_std"] == 0 and row["standardized_mean_shift"] == 0
        for row in equal
    )
    changed = _shift(cohort(0.1, 101), cohort(0.2, 23), 1)
    assert all(
        row["standardized_mean_shift"] is None and row["status"] == "zero_variance_mean_changed"
        for row in changed
    )
    # Equal means with changed variance intentionally do not constitute full drift detection.
    variance_only = _shift(cohort(0, 101), cohort(-1, 1) + cohort(1, 1), 1)
    assert all(row["standardized_mean_shift"] == 0 for row in variance_only)


def test_zero_baseline_loss_cases_no_infinity(saved, tmp_path):
    scenario = copy.deepcopy(saved["scenario"])
    for session in scenario["sessions"]:
        for bar in session["bars"]:
            bar["open"] = bar["close"] = "100"
    result = run_research(scenario, saved["configuration"], tmp_path)
    report = observe(result)
    assert all(
        item["mse_ratio"] == 1 and item["mse_ratio_status"] == "both_zero_equal_ratio_one"
        for item in report["horizons"].values()
    )
    final = result["final_results"]["5"]
    for prediction in final["predictions"]:
        prediction["prediction"] = 0.1
    final["selected_score"] = {"mse": 0.01, "mae": 0.1, "direction_accuracy": 0.0, "rows": 30}
    rehash(result)
    report = observe(result)
    assert report["horizons"]["5"]["mse_ratio"] is None
    assert "baseline_zero_selected_nonzero" in {
        a["code"] for a in report["horizons"]["5"]["alerts"]
    }
    assert "Infinity" not in canonical(report)


def test_resigned_removed_entire_evaluation_sample_cannot_shrink_cohort(saved):
    result = copy.deepcopy(saved)
    final = result["final_results"]["5"]
    deleted = final["evaluation_indices"].pop(0)
    deleted_key = (deleted["decision_index"], deleted["code"])
    table = result["feature_label_tables"]["5"]
    table["samples"] = [
        row for row in table["samples"] if (row["decision_index"], row["code"]) != deleted_key
    ]
    rows = {(row["decision_index"], row["code"]): row for row in table["samples"]}
    for prediction_field, score_field in (
        ("predictions", "selected_score"),
        ("baseline_predictions", "baseline_score"),
    ):
        final[prediction_field] = [
            row
            for row in final[prediction_field]
            if (row["decision_index"], row["code"]) != deleted_key
        ]
        labels = np.asarray(
            [rows[(row["decision_index"], row["code"])]["label"] for row in final[prediction_field]]
        )
        predictions = np.asarray([row["prediction"] for row in final[prediction_field]])
        final[score_field] = {
            "mse": float(np.mean((predictions - labels) ** 2)),
            "mae": float(np.mean(abs(predictions - labels))),
            "direction_accuracy": float(np.mean(np.sign(predictions) == np.sign(labels))),
            "rows": len(labels),
        }
    rehash(result)
    with pytest.raises(ValueError, match="^Research monitoring source unavailable or invalid$"):
        observe(result)


def test_resigned_label_must_match_saved_raw_open_source(saved):
    result = copy.deepcopy(saved)
    result["feature_label_tables"]["5"]["samples"][0]["label"] += 0.5
    rehash(result)
    with pytest.raises(ValueError, match="^Research monitoring source unavailable or invalid$"):
        observe(result)


def test_historical_incomplete_costs_are_not_replayed_or_hidden(saved):
    result = copy.deepcopy(saved)
    result["final_results"]["5"]["cost_evaluation_complete"] = False
    result["cost_evaluation_complete"] = False
    rehash(result)
    report = observe(result)
    assert report["horizons"]["5"]["metrics_available"]
    assert "historical_cost_evaluation_incomplete" in {
        a["code"] for a in report["horizons"]["5"]["alerts"]
    }
