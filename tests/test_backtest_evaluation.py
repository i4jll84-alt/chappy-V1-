import json

import pytest

from trading_system.backtest.evaluation import ExperimentRegistry, walk_forward_splits


def test_chronological_purge_and_unseen_final_tail():
    ends = [min(i + 3, 29) for i in range(30)]
    result = walk_forward_splits(30, 8, 4, final_size=6, gap=2, label_end=ends)
    final = set(result["final_test"])
    assert final == set(range(24, 30))
    seen = set()
    for fold in result["folds"]:
        assert max(ends[i] for i in fold["train"]) < min(fold["test"])
        assert max(fold["train"]) < min(fold["test"]) - 2
        assert not final.intersection(fold["train"] + fold["test"])
        assert max(ends[i] for i in fold["test"]) < 22
        assert not seen.intersection(fold["test"])
        seen.update(fold["test"])
    assert max(ends[i] for i in result["final_train"]) < 24


def test_tuning_labels_cannot_reach_final_holdout():
    ends = list(range(12))
    ends[8] = 10
    result = walk_forward_splits(12, 3, 2, final_size=3, label_end=ends)
    assert result["purged_development_test"] == [8]
    assert all(ends[i] < 9 for fold in result["folds"] for i in fold["test"])


@pytest.mark.parametrize("manifest", [{}, {"final_candidate": None}, []])
def test_registry_requires_explicit_candidate(tmp_path, manifest):
    with pytest.raises(ValueError, match="final_candidate"):
        ExperimentRegistry(tmp_path, manifest)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"gap": -1},
        {"gap": True},
        {"final_size": 25},
        {"label_end": [0]},
        {"label_end": [29] * 30},
        {"label_end": [-1] * 30},
    ],
)
def test_invalid_or_fully_purged_protocol_rejected(kwargs):
    params = {"final_size": 6, **kwargs}
    with pytest.raises(ValueError):
        walk_forward_splits(30, 8, 4, **params)


def test_registry_preserves_trials_and_consumes_final_once(tmp_path):
    candidate = {"lookback": 3}
    manifest = {"data_hash": "fixture", "final_candidate": candidate}
    registry = ExperimentRegistry(tmp_path, manifest)
    assert registry.record_trial({"lookback": 2}, {"status": "rejected"}) == 1
    assert registry.record_trial(candidate, {"status": "complete"}) == 2
    calls = []
    assert registry.evaluate_final(candidate, lambda: calls.append(1) or {"value": 7}) == {
        "value": 7
    }
    reopened = ExperimentRegistry(tmp_path, manifest)
    with pytest.raises(ValueError, match="consumed"):
        reopened.evaluate_final(candidate, lambda: calls.append(2))
    with pytest.raises(ValueError, match="frozen"):
        reopened.record_trial({}, {})
    assert calls == [1]
    assert len(list(registry.root.glob("trial-*.json"))) == 2


def test_failed_final_stays_consumed_without_logging_exception_text(tmp_path):
    registry = ExperimentRegistry(tmp_path, {"final_candidate": {"lookback": 3}})

    def fail():
        raise RuntimeError("sensitive-value")

    with pytest.raises(RuntimeError):
        registry.evaluate_final({"lookback": 3}, fail)
    saved = (registry.root / "final_result.json").read_text()
    assert "sensitive-value" not in saved
    assert json.loads(saved)["error_type"] == "RuntimeError"
    with pytest.raises(ValueError, match="consumed"):
        registry.evaluate_final({"lookback": 3}, lambda: {})


def test_changed_candidate_rejected_before_final_claim(tmp_path):
    registry = ExperimentRegistry(tmp_path, {"final_candidate": {"lookback": 3}})
    with pytest.raises(ValueError, match="frozen"):
        registry.evaluate_final({"lookback": 4}, lambda: {})
    assert not (registry.root / "final_claim.json").exists()
