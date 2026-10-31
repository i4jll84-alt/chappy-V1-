"""Offline input checks and full blocker reporting; no backtest or broker calls."""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from trading_system.historical_inputs import verify_bundle, write_new
from trading_system.storage.store import digest


def inspect_bundle(bundle, decision_at):
    """Read a verified observation bundle without choosing successful subsets."""
    bundle = Path(bundle).resolve()
    report = verify_bundle(bundle, decision_at)
    match_bytes = (bundle / "rights-matches.json").read_bytes()
    expected_hash = next(
        x["sha256"] for x in report["artifacts"] if x["path"] == "rights-matches.json"
    )
    if digest(match_bytes) != expected_hash:
        raise ValueError("bundle_changed_after_verification")
    matches = json.loads(match_bytes)
    ordinary = [item for item in matches if item["ordinary_share_candidate"]]
    pending = [item for item in ordinary if item["match_state"] != "code_date_type_direction_match"]
    states = dict(Counter(item["match_state"] for item in matches))
    ordinary_states = dict(Counter(item["match_state"] for item in ordinary))
    if (
        len(matches) != report["price_events"]
        or states != report["match_states"]
        or ordinary_states != report["ordinary_match_states"]
    ):
        raise ValueError("bundle_rights_summary_mismatch")
    return {
        "version": "historical_preflight_v1",
        "state": "blocked_missing_historical_inputs",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "decision_at": decision_at.isoformat(),
        "bundle": str(bundle),
        "bundle_version": report["version"],
        "bundle_available_at": report["available_at"],
        "period": {"start": report["start"], "end": report["end"]},
        "integrity_verified": True,
        "artifacts_verified": len(report["artifacts"]),
        "sources_verified": len(report["sources"]),
        "price_events": len(matches),
        "match_states": states,
        "ordinary_match_states": ordinary_states,
        "unresolved_ordinary_rights": pending,
        "gaps": list(report["gaps"]),
        "historical_backtest_ready": False,
        "real_execution_enabled": False,
        "market_performance_evaluated": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--decision-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output_already_exists")
    # Never put a check result into an immutable input bundle.
    if args.output.resolve().is_relative_to(args.bundle.resolve()):
        parser.error("output_must_be_outside_input_bundle")
    try:
        report = inspect_bundle(args.bundle, args.decision_at)
        exit_code = 3
    except (ValueError, OSError, KeyError, TypeError) as exc:
        report = {
            "state": "verification_failed",
            "error": str(exc),
            "integrity_verified": False,
            "historical_backtest_ready": False,
            "real_execution_enabled": False,
            "market_performance_evaluated": False,
        }
        exit_code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_new(args.output, report)
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "unresolved_ordinary_rights"},
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
