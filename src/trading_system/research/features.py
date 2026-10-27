"""Causal price-only features and next-open targets on original session indexes."""

import numpy as np

from trading_system.backtest.models import Scenario
from trading_system.storage.store import canonical, digest

HORIZONS = (5, 10, 20, 60)
FEATURES = (
    "return_1",
    "return_5",
    "return_20",
    "close_to_mean_5",
    "close_to_mean_20",
    "volatility_20",
)
VERSION = "causal_raw_prices_next_open_targets_v1"


def build_samples(scenario: Scenario | dict, horizon: int) -> dict:
    scenario = Scenario.model_validate(scenario)
    if type(horizon) is not int or horizon not in HORIZONS:
        raise ValueError("Only explicit 5/10/20/60 session horizons are supported")
    if scenario.actions:
        raise ValueError("Model research initially requires explicit action-free synthetic data")
    if scenario.signal_policy != "momentum" or scenario.signals:
        raise ValueError("Research source must not already contain model signals")
    sessions = scenario.sessions
    bars = [{bar.code: bar for bar in session.bars} for session in sessions]
    rows, skipped = [], []
    for t, session in enumerate(sessions):
        for code in sorted(session.universe):
            reason = None
            if t < 20:
                reason = "feature_warmup_21_sessions_required"
            elif t + horizon + 1 >= len(sessions):
                reason = "full_label_horizon_unavailable"
            if reason is None:
                trailing = [bars[index][code] for index in range(t - 20, t + 1)]
                if any(bar.known_at > session.decision_at for bar in trailing):
                    reason = "trailing_feature_not_yet_known"
            if reason is None:
                entry, exit_bar = bars[t + 1][code], bars[t + horizon + 1][code]
                if entry.open is None or exit_bar.open is None:
                    reason = "label_open_unavailable"
            if reason is not None:
                skipped.append({"decision_index": t, "code": code, "reason": reason})
                continue
            prices = np.asarray([float(bar.close) for bar in trailing], dtype=np.float64)
            returns = prices[1:] / prices[:-1] - 1
            features = [prices[-1] / prices[-1 - lag] - 1 for lag in (1, 5, 20)]
            features += [prices[-1] / prices[-window:].mean() - 1 for window in (5, 20)]
            features += [returns.std(ddof=1)]
            label = float(exit_bar.open / entry.open - 1)
            if not np.isfinite(features).all() or not np.isfinite(label):
                raise ValueError("Nonfinite research feature or target")
            source = [
                {
                    "session_index": index,
                    "close": str(bars[index][code].close),
                    "known_at": bars[index][code].known_at.isoformat(),
                }
                for index in range(t - 20, t + 1)
            ]
            rows.append(
                {
                    "decision_index": t,
                    "code": code,
                    "decision_at": session.decision_at.isoformat(),
                    "feature_known_at": max(bar.known_at for bar in trailing).isoformat(),
                    "features": [float(value) for value in features],
                    "label": label,
                    "entry_index": t + 1,
                    "label_end_index": t + horizon + 1,
                    "label_known_at": max(entry.known_at, exit_bar.known_at).isoformat(),
                    "feature_source_hash": digest(canonical(source).encode()),
                    "feature_sources": source,
                }
            )
    return {
        "mode": "synthetic",
        "version": VERSION,
        "horizon_sessions": horizon,
        "feature_names": list(FEATURES),
        "samples": rows,
        "skipped": skipped,
        "target": "raw_open[t+h+1]/raw_open[t+1]-1",
        "input_hash": digest(canonical(scenario.model_dump(mode="json")).encode()),
        "benchmark_features_used": False,
        "corporate_actions_supported": False,
    }
