"""Frozen chronological candidate selection and exactly-once synthetic final evaluation."""

import json
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import numpy as np
from pydantic import Field, model_validator

from trading_system.backtest.evaluation import ExperimentRegistry
from trading_system.backtest.models import Input, Scenario
from trading_system.research.features import HORIZONS, build_samples
from trading_system.research.models import Candidate, predict_model, train_model
from trading_system.storage.store import atomic_write, canonical, digest

VERSION = "synthetic_causal_model_research_v1"


def default_candidates():
    return tuple(
        Candidate.model_validate(item)
        for item in [
            {"name": "cash_zero", "kind": "zero"},
            {"name": "train_mean", "kind": "mean"},
            {"name": "ridge_1", "kind": "ridge", "alpha": 1.0},
            {"name": "ridge_10", "kind": "ridge", "alpha": 10.0},
            {"name": "lightgbm_fixed", "kind": "lightgbm"},
        ]
    )


class ResearchConfig(Input):
    development_start: int = Field(default=180, strict=True, gt=20)
    final_start: int = Field(default=300, strict=True, gt=20)
    final_end: int = Field(default=350, strict=True, gt=20)
    minimum_relative_improvement: float = Field(default=0.01, ge=0, lt=1, allow_inf_nan=False)
    minimum_train_rows: int = Field(default=20, strict=True, ge=2)
    minimum_evaluation_rows: int = Field(default=10, strict=True, ge=2)
    candidates: tuple[Candidate, ...] = Field(default_factory=default_candidates, min_length=2)

    @model_validator(mode="after")
    def boundaries(self):
        if not self.development_start < self.final_start < self.final_end:
            raise ValueError("Development/final boundaries must be strictly chronological")
        if len({candidate.name for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate names must be unique")
        if not any(candidate.kind in {"zero", "mean"} for candidate in self.candidates):
            raise ValueError("An explicit simple baseline candidate is required")
        return self


def _training(rows, boundary, cutoff):
    return [
        row
        for row in rows
        if row["decision_index"] < boundary
        and row["label_end_index"] < boundary
        and datetime.fromisoformat(row["label_known_at"]) <= cutoff
    ]


def _fit(candidate, rows, names, cutoff, input_hash):
    return train_model(
        candidate,
        [row["features"] for row in rows],
        [row["label"] for row in rows],
        names,
        fit_at=cutoff.isoformat(),
        training_indices=[
            {
                "decision_index": row["decision_index"],
                "code": row["code"],
                "label_end_index": row["label_end_index"],
                "label_known_at": row["label_known_at"],
                "feature_known_at": row["feature_known_at"],
            }
            for row in rows
        ],
        input_hash=input_hash,
    )


def _predict(model, rows, names):
    predictions = predict_model(model, [row["features"] for row in rows], names)
    return [
        {
            "decision_index": row["decision_index"],
            "code": row["code"],
            "decision_at": row["decision_at"],
            "prediction": prediction,
            "feature_known_at": row["feature_known_at"],
            "trained_at": model["trained_at"],
            "known_at": row["decision_at"],
            "model_id": model["model_id"],
        }
        for row, prediction in zip(rows, predictions, strict=True)
    ]


def _score(predictions, rows):
    y = np.asarray([row["label"] for row in rows])
    p = np.asarray([item["prediction"] for item in predictions])
    return {
        "mse": float(np.mean((p - y) ** 2)),
        "mae": float(np.mean(np.abs(p - y))),
        "direction_accuracy": float(np.mean(np.sign(p) == np.sign(y))),
        "rows": len(rows),
    }


def _select(trials, config):
    valid = [trial for trial in trials if trial["status"] != "failed"]
    baseline = min(
        (trial for trial in valid if trial["candidate"]["kind"] in {"zero", "mean"}),
        key=lambda item: (item["validation_score"]["mse"], item["candidate"]["name"]),
    )
    learned = [trial for trial in valid if trial["candidate"]["kind"] not in {"zero", "mean"}]
    winner = (
        min(learned, key=lambda item: (item["validation_score"]["mse"], item["candidate"]["name"]))
        if learned
        else None
    )
    selected = baseline
    if winner is not None and winner["validation_score"]["mse"] < (
        baseline["validation_score"]["mse"] * (1 - config.minimum_relative_improvement)
    ):
        selected = winner
    for trial in valid:
        trial["status"] = "selected" if trial is selected else "rejected"
        trial["reason"] = (
            "validation_mse_frozen_selection"
            if trial is selected
            else "not_selected_on_development"
        )
    return selected["candidate"], baseline["candidate"]


def run_research(scenario, config: ResearchConfig | dict, registry_root: Path) -> dict:
    scenario, config = Scenario.model_validate(scenario), ResearchConfig.model_validate(config)
    if config.final_end + max(HORIZONS) >= len(scenario.sessions):
        raise ValueError(
            "Complete longest-horizon outcomes beyond the final decision window are required"
        )
    input_hash = digest(canonical(scenario.model_dump(mode="json")).encode())
    audit = ExperimentRegistry(
        Path(registry_root) / "development",
        {
            "pipeline_version": VERSION,
            "mode": "synthetic",
            "input_hash": input_hash,
            "configuration": config.model_dump(mode="json"),
            "libraries": {"numpy": version("numpy"), "lightgbm": version("lightgbm")},
            "final_candidate": {"phase": "development_audit_only"},
        },
    )
    pointer = audit.root / "frozen_experiment.json"
    if pointer.exists():
        frozen = json.loads(pointer.read_bytes())
        final_root = Path(registry_root) / frozen["experiment_id"]
        for name in ("final_result.json", "development_result.json"):
            cached_path = final_root / name
            if cached_path.exists():
                payload = json.loads(cached_path.read_bytes())
                if name == "development_result.json":
                    return payload
                if payload["status"] == "complete":
                    return payload["result"]
                raise ValueError("Final holdout previously failed and remains consumed")
        if (final_root / "final_claim.json").exists():
            raise ValueError("Final holdout previously consumed without a complete result")
    development_cutoff = scenario.sessions[config.development_start].open_at
    final_cutoff = scenario.sessions[config.final_start].open_at
    datasets, histories, selections, prepared = {}, [], {}, {}
    for horizon in HORIZONS:
        dataset = build_samples(scenario, horizon)
        datasets[str(horizon)] = dataset
        rows, names = dataset["samples"], dataset["feature_names"]
        train = _training(rows, config.development_start, development_cutoff)
        validation = [
            row
            for row in rows
            if config.development_start <= row["decision_index"]
            and row["label_end_index"] < config.final_start
            and datetime.fromisoformat(row["label_known_at"]) <= final_cutoff
        ]
        final_train = _training(rows, config.final_start, final_cutoff)
        final = [
            row for row in rows if config.final_start <= row["decision_index"] < config.final_end
        ]
        invalid_final_target = any(
            config.final_start <= row["decision_index"] < config.final_end
            and row["reason"] in {"label_open_unavailable", "full_label_horizon_unavailable"}
            for row in dataset["skipped"]
        )
        if invalid_final_target or any(
            datetime.fromisoformat(row["label_known_at"]) > scenario.sessions[-1].decision_at
            for row in final
        ):
            raise ValueError(
                "Final target availability cannot determine which prediction trades exist"
            )
        if (
            len(train) < config.minimum_train_rows
            or len(final_train) < config.minimum_train_rows
            or min(len(validation), len(final)) < config.minimum_evaluation_rows
        ):
            raise ValueError("Insufficient qualified chronological samples after purging")
        trials = []
        for candidate in config.candidates:
            trial = {
                "horizon": horizon,
                "candidate": candidate.model_dump(mode="json"),
                "train_indices": [
                    {"decision_index": row["decision_index"], "code": row["code"]} for row in train
                ],
                "validation_indices": [
                    {"decision_index": row["decision_index"], "code": row["code"]}
                    for row in validation
                ],
            }
            audit.record_trial(
                {
                    "phase": "fit_started",
                    "horizon": horizon,
                    "candidate": candidate.model_dump(mode="json"),
                },
                trial | {"status": "started"},
            )
            try:
                model = _fit(candidate, train, names, development_cutoff, input_hash)
                predictions = _predict(model, validation, names)
                trial.update(
                    status="evaluated",
                    model=model,
                    predictions=predictions,
                    validation_score=_score(predictions, validation),
                )
            except Exception as exc:
                trial.update(
                    status="failed",
                    reason="candidate_fit_or_prediction_failed",
                    error_type=type(exc).__name__,
                )
            audit.record_trial(
                {
                    "phase": "development",
                    "horizon": horizon,
                    "candidate": candidate.model_dump(mode="json"),
                },
                trial,
            )
            trials.append(trial)
        try:
            selected, baseline = _select(trials, config)
        except ValueError:
            selected, baseline = None, None
        for trial in trials:
            audit.record_trial(
                {"phase": "selection", "horizon": horizon, "candidate": trial["candidate"]},
                {"status": trial["status"], "reason": trial.get("reason")},
            )
        histories.extend(trials)
        selections[str(horizon)] = {"selected": selected, "baseline": baseline}
        prepared[str(horizon)] = (final_train, final, names)
    manifest = {
        "pipeline_version": VERSION,
        "mode": "synthetic",
        "scenario": scenario.model_dump(mode="json"),
        "input_hash": input_hash,
        "configuration": config.model_dump(mode="json"),
        "final_candidate": selections,
        "development_history_hash": digest(canonical(histories).encode()),
        "development_audit_id": audit.experiment_id,
        "protocol": "original_session_indexes_label_end_before_boundary_known_by_fit_open",
    }
    registry = ExperimentRegistry(registry_root, manifest)
    atomic_write(pointer, canonical({"experiment_id": registry.experiment_id}).encode())
    cached = registry.root / "final_result.json"
    if cached.exists():
        payload = json.loads(cached.read_bytes())
        if payload["status"] != "complete":
            raise ValueError("Final holdout previously failed and remains consumed")
        return payload["result"]
    if (registry.root / "final_claim.json").exists():
        raise ValueError("Final holdout previously consumed without a complete result")
    development_cache = registry.root / "development_result.json"
    if development_cache.exists():
        return json.loads(development_cache.read_bytes())
    for trial in histories:
        registry.record_trial(trial["candidate"] | {"horizon": trial["horizon"]}, trial)

    if any(item["selected"] is None for item in selections.values()):
        blocked = {
            "status": "blocked",
            "reason": "no_successful_simple_baseline",
            "mode": "synthetic",
            "experiment_id": registry.experiment_id,
            "input_hash": input_hash,
            "candidate_history": histories,
            "configuration": config.model_dump(mode="json"),
            "final_holdout_consumed": False,
            "deployment_enabled": False,
        }
        blocked["result_hash"] = digest(canonical(blocked).encode())
        atomic_write(development_cache, canonical(blocked).encode())
        return blocked

    def final_evaluate():
        from trading_system.research.bridge import compare_predictions

        finals = {}
        for horizon in HORIZONS:
            key = str(horizon)
            train, rows, names = prepared[key]
            selected = _fit(selections[key]["selected"], train, names, final_cutoff, input_hash)
            baseline = _fit(selections[key]["baseline"], train, names, final_cutoff, input_hash)
            predictions = _predict(selected, rows, names)
            baseline_predictions = _predict(baseline, rows, names)
            execution_end = max(row["label_end_index"] for row in rows) + 1
            comparison = compare_predictions(
                scenario, horizon, predictions, config.final_start, execution_end
            )
            baseline_costs = compare_predictions(
                scenario, horizon, baseline_predictions, config.final_start, execution_end
            )
            cost_arms = {
                "selected_prediction": comparison["prediction"],
                "selected_momentum": comparison["momentum"],
                "baseline_prediction": baseline_costs["prediction"],
                "baseline_momentum": baseline_costs["momentum"],
            }
            finals[key] = {
                "cost_evaluation_complete": all(
                    arm["status"] == "complete" for arm in cost_arms.values()
                ),
                "cost_evaluation_blocks": [
                    {"arm": name, "reason": arm.get("reason")}
                    for name, arm in cost_arms.items()
                    if arm["status"] != "complete"
                ],
                "selected_model": selected,
                "baseline_model": baseline,
                "evaluation_at": scenario.sessions[-1].decision_at.isoformat(),
                "predictions": predictions,
                "baseline_predictions": baseline_predictions,
                "selected_score": _score(predictions, rows),
                "baseline_score": _score(baseline_predictions, rows),
                "evaluation_indices": [
                    {"decision_index": row["decision_index"], "code": row["code"]} for row in rows
                ],
                "cost_comparison": comparison,
                "baseline_cost_comparison": baseline_costs,
            }
        result = {
            "status": "complete",
            "mode": "synthetic",
            "pipeline_version": VERSION,
            "experiment_id": registry.experiment_id,
            "input_hash": input_hash,
            "scenario": scenario.model_dump(mode="json"),
            "configuration": config.model_dump(mode="json"),
            "feature_label_tables": datasets,
            "candidate_history": histories,
            "selection": selections,
            "final_results": finals,
            "cost_evaluation_complete": all(
                item["cost_evaluation_complete"] for item in finals.values()
            ),
            "assumptions": {
                "historical_market_validity": False,
                "deployment_enabled": False,
                "selection_threshold_is_statistical_significance": False,
                "final_evaluation": "single_claim_cached_on_repeat",
                "prediction_availability": "logical_decision_time_no_inference_latency",
                "all_price_features": "raw_action_free_only",
                "real_topix_or_risk_adjusted_targets": False,
            },
        }
        result["result_hash"] = digest(canonical(result).encode())
        return result

    return registry.evaluate_final(selections, final_evaluate)
