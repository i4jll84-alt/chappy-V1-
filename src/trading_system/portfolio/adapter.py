"""Import dated synthetic forecasts; never infer account or broker evidence."""

import math
from datetime import datetime

from trading_system.backtest.models import Scenario
from trading_system.point_in_time.policy import aware_utc
from trading_system.portfolio.models import CandidateSignal
from trading_system.storage.store import canonical, digest


def candidates_from_research(
    result: dict, decision_at: datetime, horizon_strategies: dict[int, str]
) -> tuple[CandidateSignal, ...]:
    """Use forecast signs from an intact completed synthetic research artifact.

    Hashes detect accidental changes, not authenticity. Research completion and
    final scores are not historical availability evidence; this is synthetic replay.
    Strategy/horizon mapping must be fixed externally, not selected by final scores.
    """
    decision_at = aware_utc(decision_at)
    if (
        result.get("mode") != "synthetic"
        or result.get("status") != "complete"
        or result.get("cost_evaluation_complete") is not True
    ):
        raise ValueError("Only completed synthetic research can be replayed")
    content = {key: value for key, value in result.items() if key != "result_hash"}
    if digest(canonical(content).encode()) != result.get("result_hash"):
        raise ValueError("Research result hash mismatch")
    if (
        not horizon_strategies
        or any(type(h) is not int or h not in (5, 10, 20, 60) for h in horizon_strategies)
        or any(not isinstance(s, str) or not s.strip() for s in horizon_strategies.values())
        or len(set(horizon_strategies.values())) != len(horizon_strategies)
    ):
        raise ValueError("Explicit distinct horizon/strategy mapping required")
    scenario = Scenario.model_validate(result["scenario"])
    if digest(canonical(scenario.model_dump(mode="json")).encode()) != result["input_hash"]:
        raise ValueError("Research scenario hash mismatch")
    indexes = [
        i for i, session in enumerate(scenario.sessions) if session.decision_at == decision_at
    ]
    if len(indexes) != 1:
        raise ValueError("Decision must match one original session exactly")
    index = indexes[0]
    session = scenario.sessions[index]
    candidates = []
    for horizon, strategy in sorted(horizon_strategies.items()):
        final = result["final_results"][str(horizon)]
        if final["cost_evaluation_complete"] is not True:
            raise ValueError("Incomplete horizon evaluation")
        model = final["selected_model"]
        model_content = {key: value for key, value in model.items() if key != "model_id"}
        if (
            digest(canonical(model_content).encode()) != model["model_id"]
            or model["input_hash"] != result["input_hash"]
            or model["mode"] != "synthetic"
            or model["deployment_enabled"] is not False
        ):
            raise ValueError("Invalid selected model provenance")
        seen = set()
        for record in final["predictions"]:
            if type(record["decision_index"]) is not int:
                raise ValueError("Invalid prediction index")
            record_at = aware_utc(datetime.fromisoformat(record["decision_at"]))
            if record["decision_index"] != index and record_at != decision_at:
                continue
            code, value = record["code"], record["prediction"]
            if (
                record["decision_index"] != index
                or record_at != decision_at
                or code not in session.universe
                or code in seen
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or record["model_id"] != model["model_id"]
                or record["trained_at"] != model["trained_at"]
            ):
                raise ValueError("Invalid dated forecast provenance")
            seen.add(code)
            known = aware_utc(datetime.fromisoformat(record["known_at"]))
            feature = aware_utc(datetime.fromisoformat(record["feature_known_at"]))
            trained = aware_utc(datetime.fromisoformat(record["trained_at"]))
            if not trained <= known <= decision_at or feature > known:
                raise ValueError("Forecast contains future information")
            candidates.append(
                CandidateSignal(
                    strategy=strategy,
                    code=code,
                    direction=1 if value > 0 else (-1 if value < 0 else 0),
                    decision_at=decision_at,
                    known_at=known,
                    feature_known_at=feature,
                    trained_at=trained,
                    model_id=model["model_id"],
                    source_result_hash=result["result_hash"],
                )
            )
        if not seen:
            raise ValueError("Requested horizon has no forecast at decision")
    return tuple(sorted(candidates, key=lambda candidate: (candidate.strategy, candidate.code)))
