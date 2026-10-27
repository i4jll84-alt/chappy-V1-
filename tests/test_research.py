import copy
import json
from datetime import datetime, timedelta

import numpy as np
import pytest
from pydantic import ValidationError

from trading_system import cli
from trading_system.backtest.engine import run_backtest
from trading_system.research import pipeline
from trading_system.research.benchmark import model_scenario
from trading_system.research.features import FEATURES, build_samples
from trading_system.research.models import predict_model, train_model
from trading_system.research.pipeline import ResearchConfig, run_research


@pytest.fixture(scope="module")
def source():
    data = model_scenario().model_dump(mode="json")
    data["declared_codes"] = ["SYN_LONG"]
    data["sessions"] = data["sessions"][:310]
    for session in data["sessions"]:
        session["bars"] = session["bars"][:1]
        session["rules"] = session["rules"][:1]
        session["universe"] = ["SYN_LONG"]
    return data


@pytest.fixture
def config():
    return {
        "development_start": 120,
        "final_start": 210,
        "final_end": 240,
        "minimum_train_rows": 10,
        "minimum_evaluation_rows": 3,
        "candidates": [
            {"name": "zero", "kind": "zero"},
            {"name": "mean", "kind": "mean"},
            {"name": "ridge", "kind": "ridge", "alpha": 1.0},
        ],
    }


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("ml")


@pytest.mark.parametrize("horizon", [5, 10, 20, 60])
def test_causal_feature_and_unshortened_next_open_label(source, horizon):
    data = build_samples(source, horizon)
    row = next(row for row in data["samples"] if row["decision_index"] == 20)
    assert row["entry_index"] == 21 and row["label_end_index"] == 21 + horizon
    entry = float(source["sessions"][21]["bars"][0]["open"])
    exit_open = float(source["sessions"][21 + horizon]["bars"][0]["open"])
    assert row["label"] == pytest.approx(exit_open / entry - 1)
    assert all(r["decision_index"] + horizon + 1 < len(source["sessions"]) for r in data["samples"])
    assert any(r["reason"] == "full_label_horizon_unavailable" for r in data["skipped"])
    assert len(row["feature_sources"]) == 21
    assert datetime.fromisoformat(row["feature_known_at"]) <= datetime.fromisoformat(
        row["decision_at"]
    )


def test_future_values_and_undated_benchmark_cannot_change_past_features(source):
    first = build_samples(source, 5)
    changed = copy.deepcopy(source)
    changed["sessions"][250]["bars"][0]["close"] = "99999"
    for session in changed["sessions"]:
        session["benchmark_close"] = "99999999"
    second = build_samples(changed, 5)

    def earlier(data):
        return [r["features"] for r in data["samples"] if r["decision_index"] < 250]

    assert earlier(first) == earlier(second)
    changed["sessions"][100]["bars"][0]["known_at"] = changed["sessions"][200]["decision_at"]
    skipped = build_samples(changed, 5)["skipped"]
    assert any(
        row["decision_index"] == 100 and row["reason"] == "trailing_feature_not_yet_known"
        for row in skipped
    )


@pytest.mark.parametrize(
    "kind,params", [("zero", {}), ("mean", {}), ("ridge", {"alpha": 1.0}), ("lightgbm", {})]
)
def test_safe_model_json_reload_and_independent_retraining_are_identical(kind, params):
    x = np.arange(240, dtype=float).reshape(40, 6) / 100
    y = np.sin(np.arange(40)) / 100
    candidate = {"name": kind, "kind": kind} | params
    kwargs = {
        "fit_at": "2020-01-01T00:00:00+00:00",
        "training_indices": list(range(40)),
        "input_hash": "synthetic-fixture",
    }
    first = train_model(candidate, x, y, FEATURES, **kwargs)
    second = train_model(candidate, x, y, FEATURES, **kwargs)
    assert first == second
    loaded = json.loads(json.dumps(first, allow_nan=False))
    assert predict_model(first, x, FEATURES) == predict_model(loaded, x, FEATURES)
    if kind == "lightgbm":
        assert isinstance(first["native_model"], str)
        assert first["library_versions"]["lightgbm"] == "4.7.0"
        assert first["parameters"]["num_threads"] == 1
    loaded["scaler_mean"][0] += 1
    with pytest.raises(ValueError, match="hash"):
        predict_model(loaded, x, FEATURES)
    with pytest.raises(ValueError, match="order"):
        predict_model(first, x, list(reversed(FEATURES)))


def test_training_shapes_reject_multioutput_and_nonfinite():
    kwargs = {"fit_at": "2020-01-01T00:00:00+00:00", "training_indices": [0, 1], "input_hash": "x"}
    with pytest.raises(ValueError):
        train_model({"name": "mean", "kind": "mean"}, [[1], [2]], [[1], [2]], ["x"], **kwargs)
    with pytest.raises(ValueError):
        train_model(
            {"name": "mean", "kind": "mean"}, [[1], [float("inf")]], [1, 2], ["x"], **kwargs
        )


def test_pipeline_purges_labels_fits_scaler_on_train_and_caches_final_once(
    source, config, tmp_path, monkeypatch
):
    result = run_research(source, config, tmp_path)
    assert result["status"] == "complete" and result["cost_evaluation_complete"]
    assert len(result["candidate_history"]) == 12
    assert any(trial["status"] == "rejected" for trial in result["candidate_history"])
    tables = result["feature_label_tables"]
    for trial in result["candidate_history"]:
        rows = {
            (row["decision_index"], row["code"]): row
            for row in tables[str(trial["horizon"])]["samples"]
        }
        train = [rows[(item["decision_index"], item["code"])] for item in trial["train_indices"]]
        assert all(row["label_end_index"] < config["development_start"] for row in train)
        assert np.allclose(
            trial["model"]["scaler_mean"], np.mean([r["features"] for r in train], axis=0)
        )
        assert all(
            rows[(item["decision_index"], item["code"])]["label_end_index"] < config["final_start"]
            for item in trial["validation_indices"]
        )
    for final in result["final_results"].values():
        assert all(
            item["label_end_index"] < config["final_start"]
            for item in final["selected_model"]["training_indices"]
        )
        assert all(
            datetime.fromisoformat(item["label_known_at"])
            <= datetime.fromisoformat(final["selected_model"]["trained_at"])
            for item in final["selected_model"]["training_indices"]
        )
    monkeypatch.setattr(
        pipeline, "train_model", lambda *_, **__: pytest.fail("Cached final retrained model")
    )
    assert run_research(source, config, tmp_path) == result
    assert len(list(tmp_path.glob("*/final_claim.json"))) == 1


def test_final_extremes_do_not_change_validation_selection_or_prefinal_scaler(
    source, config, tmp_path
):
    first = run_research(source, config, tmp_path / "one")
    changed = copy.deepcopy(source)
    for session in changed["sessions"][config["final_start"] :]:
        session["bars"][0]["close"] = str(float(session["bars"][0]["close"]) * 3)
        session["bars"][0]["open"] = str(float(session["bars"][0]["open"]) * 3)
    second = run_research(changed, config, tmp_path / "two")
    assert first["selection"] == second["selection"]
    for a, b in zip(first["candidate_history"], second["candidate_history"], strict=True):
        assert a["validation_score"] == b["validation_score"]
        for key in ("scaler_mean", "scaler_scale", "coefficients", "intercept"):
            assert a["model"][key] == b["model"][key]
    for key in first["final_results"]:
        a, b = (
            first["final_results"][key]["selected_model"],
            second["final_results"][key]["selected_model"],
        )
        assert a["scaler_mean"] == b["scaler_mean"]
        assert a["coefficients"] == b["coefficients"]


def test_delayed_prefinal_validation_label_is_purged_before_selection(source, config, tmp_path):
    source = copy.deepcopy(source)
    source["sessions"][190]["bars"][0]["known_at"] = source["sessions"][211]["open_at"]
    result = run_research(source, config, tmp_path)
    for trial in result["candidate_history"]:
        if trial["horizon"] == 60:
            assert all(item["decision_index"] != 129 for item in trial["validation_indices"])
    for item in result["final_results"]["60"]["selected_model"]["training_indices"]:
        assert item["decision_index"] != 129


@pytest.mark.parametrize("mode", ["missing_exit", "late_label"])
def test_final_target_availability_cannot_silently_remove_a_prediction_trade(
    source, config, tmp_path, mode
):
    source = copy.deepcopy(source)
    target = source["sessions"][216]["bars"][0]  # t=210,h=5 => valid entry211, exit216.
    if mode == "missing_exit":
        target["open"] = None
    else:
        target["known_at"] = (
            datetime.fromisoformat(source["sessions"][-1]["decision_at"]) + timedelta(days=1)
        ).isoformat()
    with pytest.raises(ValueError, match="Final target availability"):
        run_research(source, config, tmp_path)
    assert not list(tmp_path.glob("*/final_claim.json"))


def test_candidate_failures_and_all_baseline_failures_remain_in_audit(
    source, config, tmp_path, monkeypatch
):
    original = pipeline.train_model

    def fail_ridge(candidate, *args, **kwargs):
        if (candidate.kind if hasattr(candidate, "kind") else candidate["kind"]) == "ridge":
            raise ArithmeticError("do not persist exception text")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(pipeline, "train_model", fail_ridge)
    result = run_research(source, config, tmp_path / "partial")
    assert sum(trial["status"] == "failed" for trial in result["candidate_history"]) == 4
    assert any(
        "error_type" in json.loads(path.read_bytes())["result"]
        for path in (tmp_path / "partial/development").glob("*/trial-*.json")
    )

    def fail_all(*args, **kwargs):
        raise ArithmeticError("do not persist exception text")

    monkeypatch.setattr(pipeline, "train_model", fail_all)
    blocked = run_research(source, config, tmp_path / "all")
    assert blocked["status"] == "blocked" and not blocked["final_holdout_consumed"]
    assert len(blocked["candidate_history"]) == 12
    assert not list((tmp_path / "all").glob("*/final_claim.json"))


def test_interrupt_preserves_completed_and_started_development_trials(
    source, config, tmp_path, monkeypatch
):
    original, count = pipeline.train_model, 0

    def interrupt(candidate, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise KeyboardInterrupt()
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(pipeline, "train_model", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_research(source, config, tmp_path)
    events = [
        json.loads(path.read_bytes()) for path in (tmp_path / "development").glob("*/trial-*.json")
    ]
    assert any(item["result"]["status"] == "started" for item in events)
    assert any(item["result"]["status"] == "evaluated" for item in events)
    assert not list(tmp_path.glob("*/final_claim.json"))


def test_failed_final_stays_consumed(source, config, tmp_path, monkeypatch):
    from trading_system.research import bridge

    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated final evaluation failure")

    monkeypatch.setattr(bridge, "compare_predictions", fail)
    with pytest.raises(RuntimeError):
        run_research(source, config, tmp_path)
    with pytest.raises(ValueError, match="consumed"):
        run_research(source, config, tmp_path)
    assert calls == 1


def test_cost_failure_is_reported_separately_from_completed_workflow(
    source, config, tmp_path, monkeypatch
):
    from trading_system.research import bridge

    original = bridge.compare_predictions

    def blocked(*args, **kwargs):
        result = original(*args, **kwargs)
        result["prediction"].update(
            status="blocked", reason="synthetic_test_margin_breach", metrics=None
        )
        return result

    monkeypatch.setattr(bridge, "compare_predictions", blocked)
    result = run_research(source, config, tmp_path)
    assert result["status"] == "complete" and not result["cost_evaluation_complete"]
    assert all(not final["cost_evaluation_complete"] for final in result["final_results"].values())


def test_simple_baseline_is_retained_without_threshold_improvement():
    config = ResearchConfig(minimum_relative_improvement=0.1)
    trials = [
        {
            "candidate": {"name": "mean", "kind": "mean"},
            "status": "evaluated",
            "validation_score": {"mse": 1.0},
        },
        {
            "candidate": {"name": "ridge", "kind": "ridge", "alpha": 1},
            "status": "evaluated",
            "validation_score": {"mse": 0.95},
        },
    ]
    selected, _ = pipeline._select(trials, config)
    assert selected["kind"] == "mean"


def test_supplied_empty_is_cash_and_temporal_signal_proof_is_validated(source):
    small = copy.deepcopy(source)
    small["sessions"] = small["sessions"][:25]
    small["signal_policy"], small["signals"] = "supplied", []
    assert run_backtest(small)["orders"] == []
    decision = small["sessions"][20]["decision_at"]
    small["signals"] = [
        {
            "code": "SYN_LONG",
            "decision_at": decision,
            "known_at": decision,
            "feature_cutoff_at": decision,
            "trained_at": decision,
            "direction": 1,
            "model_id": "synthetic-proof",
        }
    ]
    assert run_backtest(small)["orders"][0]["status"] == "filled"
    for key in ("trained_at", "feature_cutoff_at", "known_at"):
        bad = copy.deepcopy(small)
        bad["signals"][0][key] = bad["sessions"][21]["decision_at"]
        with pytest.raises(ValidationError):
            run_backtest(bad)


def test_model_cli_never_loads_acquisition_settings_or_credentials(
    source, config, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(cli.Settings, "load", lambda *_: pytest.fail("Acquisition settings read"))
    monkeypatch.setattr(cli, "api_key", lambda: pytest.fail("API key read"))
    scenario_file, config_file, output = (
        tmp_path / "input.json",
        tmp_path / "config.json",
        tmp_path / "result.json",
    )
    scenario_file.write_text(json.dumps(source))
    config_file.write_text(json.dumps(config))
    args = [
        "model-benchmark",
        "--scenario",
        str(scenario_file),
        "--research-config",
        str(config_file),
        "--registry-root",
        str(tmp_path / "registry"),
        "--output",
        str(output),
    ]
    assert cli.main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["cost_evaluation_complete"]
    first = json.loads(output.read_bytes())
    assert cli.main(args) == 0
    assert json.loads(output.read_bytes()) == first
    capsys.readouterr()
    source = copy.deepcopy(source)
    source["mode"] = "historical"
    scenario_file.write_text(json.dumps(source))
    assert cli.main(args) == 2
