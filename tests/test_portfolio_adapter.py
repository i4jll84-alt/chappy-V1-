import copy
from datetime import datetime, timedelta

import pytest

from trading_system.portfolio.adapter import candidates_from_research
from trading_system.portfolio.benchmark import synthetic_plan
from trading_system.portfolio.planner import plan_portfolio
from trading_system.research.benchmark import model_scenario
from trading_system.research.pipeline import run_research
from trading_system.storage.store import canonical, digest


@pytest.fixture(scope="module")
def research(tmp_path_factory):
    source = model_scenario().model_dump(mode="json")
    source["sessions"] = source["sessions"][:310]
    return run_research(
        source,
        {
            "development_start": 120,
            "final_start": 210,
            "final_end": 240,
            "minimum_train_rows": 10,
            "minimum_evaluation_rows": 3,
            "candidates": [{"name": "zero", "kind": "zero"}, {"name": "mean", "kind": "mean"}],
        },
        tmp_path_factory.mktemp("pa"),
    )


def decision(result):
    return datetime.fromisoformat(result["scenario"]["sessions"][210]["decision_at"])


def resign(result):
    result["result_hash"] = digest(
        canonical({key: value for key, value in result.items() if key != "result_hash"}).encode()
    )
    return result


def test_research_forecasts_keep_sign_and_lineage(research):
    candidates = candidates_from_research(
        research, decision(research), {5: "short", 20: "swing", 60: "medium"}
    )
    assert len(candidates) == 6
    assert {c.strategy for c in candidates} == {"short", "swing", "medium"}
    for candidate in candidates:
        assert candidate.source_result_hash == research["result_hash"]
        assert candidate.known_at <= candidate.decision_at
        assert candidate.confidence is None
    assert candidates == candidates_from_research(
        research, decision(research), {60: "medium", 5: "short", 20: "swing"}
    )


@pytest.mark.parametrize("mapping", [{}, {10: "swing", 20: "swing"}, {1: "short"}, {5: ""}])
def test_invalid_mapping_rejected(research, mapping):
    with pytest.raises(ValueError):
        candidates_from_research(research, decision(research), mapping)


def test_exact_decision_and_hash_required(research):
    with pytest.raises(ValueError):
        candidates_from_research(research, decision(research) + timedelta(seconds=1), {5: "short"})
    changed = copy.deepcopy(research)
    changed["final_results"]["5"]["predictions"][0]["prediction"] += 1
    with pytest.raises(ValueError, match="hash"):
        candidates_from_research(changed, decision(research), {5: "short"})


@pytest.mark.parametrize("failure", ["future", "model", "duplicate", "missing", "index", "bool"])
def test_invalid_forecast_rejected_even_with_updated_outer_hash(research, failure):
    changed = copy.deepcopy(research)
    records = changed["final_results"]["5"]["predictions"]
    record = records[0]
    if failure == "future":
        record["known_at"] = (decision(changed) + timedelta(seconds=1)).isoformat()
    elif failure == "model":
        record["model_id"] = "other"
    elif failure == "duplicate":
        records.append(copy.deepcopy(record))
    elif failure == "missing":
        records[:] = [r for r in records if r["decision_index"] != 210]
    elif failure == "index":
        record["decision_index"] += 1
    elif failure == "bool":
        record["prediction"] = True
    with pytest.raises(ValueError):
        candidates_from_research(resign(changed), decision(changed), {5: "short"})


@pytest.mark.parametrize(
    "field,value", [("mode", "real"), ("status", "blocked"), ("cost_evaluation_complete", False)]
)
def test_incomplete_or_real_result_refused(research, field, value):
    changed = copy.deepcopy(research)
    changed[field] = value
    with pytest.raises(ValueError):
        candidates_from_research(resign(changed), decision(changed), {5: "short"})


@pytest.mark.parametrize("failure", ["scenario", "model", "horizon_incomplete"])
def test_inner_provenance_required(research, failure):
    changed = copy.deepcopy(research)
    if failure == "scenario":
        changed["scenario"]["source_label"] = "changed input"
    elif failure == "model":
        changed["final_results"]["5"]["selected_model"]["trained_at"] = decision(
            changed
        ).isoformat()
    else:
        changed["final_results"]["5"]["cost_evaluation_complete"] = False
    with pytest.raises(ValueError):
        candidates_from_research(resign(changed), decision(changed), {5: "short"})


def test_completed_research_connects_to_dated_synthetic_risk_plan(research):
    at = decision(research)
    plan = synthetic_plan().model_dump(mode="json")
    plan["decision_at"] = at.isoformat()
    plan["account"].update(as_of=at.isoformat(), known_at=at.isoformat())
    codes = research["scenario"]["declared_codes"]
    for instrument, code in zip(plan["instruments"], codes, strict=True):
        instrument.update(code=code, lot_size=1)
        for field in ("mark_as_of", "mark_known_at", "rule_known_at", "valid_from"):
            instrument[field] = at.isoformat()
        instrument["valid_until"] = (at + timedelta(days=1)).isoformat()
    plan["correlations"][0].update(
        code_a=codes[0],
        code_b=codes[1],
        window_end=at.isoformat(),
        known_at=at.isoformat(),
        valid_until=(at + timedelta(days=1)).isoformat(),
    )
    plan["signals"] = [
        signal.model_dump(mode="json")
        for signal in candidates_from_research(
            research, at, {5: "short_term", 20: "swing", 60: "medium"}
        )
    ]
    result = plan_portfolio(plan)
    assert result["status"] == "complete"
    assert result["source_result_hashes"] == [research["result_hash"]]
    assert not result["execution_enabled"]
    assert len(result["input"]["signals"]) == 6
    assert result["proposals"] or result["rejections"]
    assert result == plan_portfolio(plan)
