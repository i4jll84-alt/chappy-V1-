"""Small auditable candidates with train-only scaling and safe JSON model artifacts."""

from datetime import datetime

import numpy as np
from pydantic import Field, model_validator

from trading_system.backtest.models import Input
from trading_system.point_in_time.policy import aware_utc
from trading_system.storage.store import canonical, digest


class Candidate(Input):
    name: str = Field(min_length=1)
    kind: str
    alpha: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def allowed(self):
        if self.kind not in {"zero", "mean", "ridge", "lightgbm"}:
            raise ValueError("Unsupported research candidate")
        if (self.kind == "ridge") != (self.alpha is not None):
            raise ValueError("Only Ridge requires explicit positive alpha")
        return self


def train_model(candidate, x, y, feature_names, *, fit_at, training_indices, input_hash):
    candidate = Candidate.model_validate(candidate)
    fit_at = aware_utc(datetime.fromisoformat(fit_at)).isoformat()
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if (
        y.ndim != 1
        or x.ndim != 2
        or x.shape[1] != len(feature_names)
        or len(x) != len(y)
        or not len(y)
    ):
        raise ValueError("Training rows/features/targets must align and be nonempty")
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("Training values must be finite")
    scaler_mean = x.mean(axis=0)
    scaler_scale = x.std(axis=0)
    constant_columns = scaler_scale == 0
    scaler_scale[constant_columns] = 1
    scaled = (x - scaler_mean) / scaler_scale
    intercept, coefficients, native = 0.0, None, None
    library = {"numpy": np.__version__}
    params = candidate.model_dump(mode="json")
    if candidate.kind == "mean":
        intercept = float(y.mean())
    elif candidate.kind == "ridge":
        intercept = float(y.mean())
        coefficients = np.linalg.solve(
            scaled.T @ scaled + candidate.alpha * np.eye(x.shape[1]),
            scaled.T @ (y - intercept),
        ).tolist()
    elif candidate.kind == "lightgbm":
        import lightgbm as lgb

        library["lightgbm"] = lgb.__version__
        params = {
            "objective": "regression_l2",
            "learning_rate": 0.05,
            "num_leaves": 7,
            "max_depth": 3,
            "min_data_in_leaf": 8,
            "verbosity": -1,
            "num_threads": 1,
            "seed": 1729,
            "deterministic": True,
            "force_col_wise": True,
            "feature_fraction": 1.0,
            "bagging_fraction": 1.0,
            "bagging_freq": 0,
        }
        model = lgb.train(params, lgb.Dataset(scaled, label=y), num_boost_round=30)
        native = model.model_to_string()
        params = params | {"num_boost_round": 30}
    artifact = {
        "format": "safe_json_native_model_v1",
        "mode": "synthetic",
        "candidate": candidate.model_dump(mode="json"),
        "feature_names": list(feature_names),
        "scaler_mean": scaler_mean.tolist(),
        "scaler_scale": scaler_scale.tolist(),
        "constant_columns": constant_columns.tolist(),
        "intercept": intercept,
        "coefficients": coefficients,
        "native_model": native,
        "parameters": params,
        "library_versions": library,
        "trained_at": fit_at,
        "training_indices": training_indices,
        "input_hash": input_hash,
        "deployment_enabled": False,
    }
    artifact["model_id"] = digest(canonical(artifact).encode())
    return artifact


def predict_model(artifact, x, feature_names):
    if artifact.get("format") != "safe_json_native_model_v1" or artifact.get("mode") != "synthetic":
        raise ValueError("Unsupported model artifact")
    content = {key: value for key, value in artifact.items() if key != "model_id"}
    if digest(canonical(content).encode()) != artifact.get("model_id"):
        raise ValueError("Model artifact hash mismatch")
    if list(feature_names) != artifact["feature_names"]:
        raise ValueError("Feature order mismatch")
    x = np.asarray(x, dtype=float)
    if x.ndim != 2 or x.shape[1] != len(feature_names) or not np.isfinite(x).all():
        raise ValueError("Prediction features must align and be finite")
    scale = np.asarray(artifact["scaler_scale"])
    mean = np.asarray(artifact["scaler_mean"])
    if scale.shape != (x.shape[1],) or mean.shape != scale.shape or np.any(scale <= 0):
        raise ValueError("Invalid scaler shape or values")
    scaled = (x - mean) / scale
    kind = artifact["candidate"]["kind"]
    if kind == "lightgbm":
        import lightgbm as lgb

        if lgb.__version__ != artifact["library_versions"]["lightgbm"]:
            raise ValueError("Native model library version differs")
        predictions = lgb.Booster(model_str=artifact["native_model"]).predict(scaled, num_threads=1)
    elif kind == "ridge":
        predictions = scaled @ np.asarray(artifact["coefficients"]) + artifact["intercept"]
    elif kind in {"zero", "mean"}:
        predictions = np.full(len(x), artifact["intercept"])
    else:
        raise ValueError("Unknown model kind")
    if not np.isfinite(predictions).all():
        raise ValueError("Nonfinite model prediction")
    return predictions.tolist()
