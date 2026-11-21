"""Offline, fixed-rule first-five-year original/approximate comparison.

IRBANK can enable the approximate branch only. The original branch still requires
the frozen TSE/restriction source manifests; missing inputs never become zero P&L.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from trading_system.brokers.locking import single_writer
from trading_system.research import irbank_compare as compare
from trading_system.research import irbank_replay as shared
from trading_system.research import swing_credit
from trading_system.research.irbank_jsf import Archive, Halt, now, save
from trading_system.research.irbank_required import Evidence, decide, prepare_candidates

SPEC = {
    "version": 1,
    "scope": "first5",
    "start": shared.ANCHOR,
    "end": shared.FIRST5_END,
    "capital": 3000000,
    "fixed_shares": 100,
    "settlement": "T+2",
    "fee_per_side": 10,
    "rate_per_side": "0.0005",
    "stress_slippage_per_side": "0.001",
    "original": "frozen_TSE_balances_restrictions_and_versions",
    "approx": "IRBANK_all_markets_later_values_blank_restrictions",
    "missing_balances": "excluded_with_counts_never_zero_filled",
}


def authorized(root):
    path = root / "acquisition-policy.json"
    if not path.exists():
        return False
    rule = shared.read(path).get("first5_comparison", {})
    return rule.get("enabled") is True and rule.get("spec") == SPEC


def inputs(project, root):
    technical = prepare_candidates(project, root)
    days = technical["sessions"]
    start, end = days.index(shared.ANCHOR), days.index(shared.FIRST5_END)
    if shared.HALT in days or len(days) != len(set(days)):
        raise ValueError("first5_calendar_invalid")
    pools = {day: technical["days"][day] for day in days[start:end]}
    codes = sorted({row["code"][:-1] for pool in pools.values() for row in pool})
    pending = []
    states = {}
    for code in codes:
        path = root / "state/first5" / (code + ".json")
        if not path.exists():
            pending.append(code)
            continue
        state = shared.read(path)
        if (state["code"], state["start"], state["end"]) != (code, "2016-11-17", shared.FIRST5_END):
            raise ValueError("first5_window_mismatch")
        if state["state"] != "complete":
            pending.append(code)
        else:
            states[code] = state
    return (
        technical,
        days,
        pools,
        states,
        {
            "state": "waiting" if pending else "ready",
            "target_codes": len(codes),
            "complete_codes": len(states),
            "remaining_codes": len(pending),
            "first_missing": pending[:10],
        },
    )


def original_sources(project, days):
    source = (
        project / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs/sources.json"
    )
    try:
        pins = shared.read(source) if source.exists() else {}
        if not isinstance(pins, dict):
            raise ValueError("original_source_manifest_invalid")
    except (ValueError, OSError) as exc:
        return {
            "state": "needs_attention",
            "reason": type(exc).__name__,
            "required_days": len(days),
            "metrics": None,
        }
    absent = []
    for day in days:
        p = project / "data/free-market/jsf-history/days" / (day + ".json")
        if not p.is_file() or str(p.relative_to(project)) not in pins:
            absent.append(day)
    return {
        "state": "waiting" if absent else "ready",
        "reason": "original_frozen_TSE_restriction_sources_missing" if absent else None,
        "required_days": len(days),
        "missing_days": len(absent),
        "first_missing": absent[:10],
        "metrics": None,
    }


def approximate_ranks(root, days, pools, states):
    needs = defaultdict(set)
    index = {d: i for i, d in enumerate(days)}
    for day, pool in pools.items():
        for row in pool:
            needs[row["code"]].update((days[index[day] - 1], day))
    evidence = Evidence(Archive(root), None, days)
    values, pins = {}, {}
    for code, dates in needs.items():
        state = states[code[:-1]]
        expected_to = shared.FIRST5_END
        if not state["pages"]:
            raise ValueError("complete_window_has_no_pages")
        terminal = False
        for end in state["pages"]:
            page = evidence.archive.read(code[:-1], end)
            if page is None:
                raise ValueError("completed_first5_page_missing")
            receipt, page_rows = page
            if terminal or receipt["to"] < expected_to:
                raise ValueError("first5_page_chain_gap")
            if not page_rows:
                terminal = True
            else:
                if page_rows[0]["date"] > expected_to:
                    raise ValueError("first5_page_chain_gap")
                expected_to = (
                    date.fromisoformat(page_rows[0]["date"]) - timedelta(days=1)
                ).isoformat()
                terminal = expected_to < "2016-11-17"
        if not terminal:
            raise ValueError("first5_window_not_covered")
        rows, _, _ = evidence.read(code[:-1])
        values[code] = {d: rows[d] for d in dates if d in rows}
        for path in sorted((root / "raw" / code[:-1]).glob("*/*")):
            if path.name in ("receipt.json", "response.json"):
                pins[str(path)] = shared.digest(path)
    ranks, selections, missing = {}, [], []
    for day, pool in pools.items():
        picks, tested = [], []
        prior = days[index[day] - 1]
        for item in pool:
            code = item["code"]
            rows = values[code]
            state = decide(rows.get(prior), rows.get(day))
            tested.append({"code": code, "state": state})
            if state == "no_data":
                missing.append({"date": day, "previous": prior, "code": code})
            elif state == "pass":
                picks.append(code)
            if len(picks) == 5:
                break
        ranks[day] = picks
        selections.append({"date": day, "selected": picks, "tested": tested})
    return ranks, {"raw": pins, "unobserved_tested": missing, "selections": selections}


def original_ranks(project, modules, days, pools):
    source = modules["structure_a_inputs"]
    pins = shared.read(
        project / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs/sources.json"
    )
    result, evidence, missing = {}, {}, []
    start, end = days.index(shared.ANCHOR), days.index(shared.FIRST5_END)
    before = None
    for day in days[start - 1 : end]:
        rows = source._credit(project, day, swing_credit, pins, {}, evidence)
        if day in pools:
            pool = [(r["code"], r["close"]) for r in pools[day]]
            picks, tested, _ = compare.select(pool, before, rows, source._passes_credit)
            result[day] = picks
            missing.extend({"date": day, **r} for r in tested if r["state"] == "no_data")
        before = rows
    return result, {"official": evidence, "unobserved_tested": missing}


def replay_branch(project, lab, modules, days, ranks, scenario, evidence):
    files = [
        Path(__file__),
        Path(shared.__file__),
        Path(compare.__file__),
        Path(__file__).with_name("irbank_jsf.py"),
        Path(__file__).with_name("irbank_required.py"),
        Path(swing_credit.__file__),
        *sorted((lab / "strategy_lab").glob("*.py")),
        *sorted((project / "scripts/swing_gap_atr").glob("*.py")),
    ]
    proof = {
        "spec": SPEC,
        "scenario": scenario,
        "evidence": evidence,
        "implementation": {str(p): shared.digest(p) for p in files},
        "ranks_sha256": hashlib.sha256(shared.encode(ranks)).hexdigest(),
    }
    identity = hashlib.sha256(shared.encode(proof)).hexdigest()
    parent = project / "data/offline-verification/irbank-first5-comparison"
    target = parent / scenario / identity
    manifest = target / "manifest.json"
    if target.exists():
        meta = shared.read(manifest)
        if meta.get("input_sha256") != identity or set(meta.get("outputs", {})) != {
            "report.json",
            "accounts.json",
            "metrics.csv",
            "daily-nav.csv",
        }:
            raise ValueError("first5_result_manifest_invalid")
        if any(shared.digest(target / name) != sha for name, sha in meta["outputs"].items()):
            raise ValueError("first5_result_changed")
        return {**shared.read(target / "report.json"), "output": str(target), "reused": True}
    first, prices = shared.replay_once(project, modules, days, ranks, shared.FIRST5_END)
    second, other = shared.replay_once(project, modules, days, ranks, shared.FIRST5_END)
    shared.verify_replays(first, second, prices, other)
    grid = [{"id": "first5", "label": "前半5年", "start": shared.ANCHOR, "end": shared.FIRST5_END}]
    calendar = days[days.index(shared.ANCHOR) : days.index(shared.FIRST5_END) + 1]
    metrics = {}
    for name, case in first.items():
        row = modules["structure_metrics"].measure(case, grid, calendar)["first5"]
        if row["coverage"] != "full":
            raise ValueError("first5_account_calendar_incomplete")
        metrics[name] = {**row, "net_profit": row["ending_nav"] - row["opening_nav"]}
    report = {
        "state": "complete",
        "scenario": scenario,
        "input_sha256": identity,
        "metrics": metrics,
        "evidence": proof,
        "price_proof": prices,
        "repeat_replay_matched": True,
        "historical_point_in_time_verified": False,
        "unobserved_tested_count": len(evidence["credit"]["unobserved_tested"]),
        "credit_coverage": "partial"
        if evidence["credit"]["unobserved_tested"]
        else "observed_tested_prefix",
        "exceptions": {k: v["counters"] for k, v in first.items()},
        "notes": [
            "後日データの研究結果。未観測残高の銘柄は選外とし件数を表示。",
            "原条件の取得不足をIRBANKで代用しない。近似を原条件の復元と呼ばない。",
            "費用控除後。税・配当・実際の約定可能性は未検証。",
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="first5-", dir=target.parent) as tmp:
        stage = Path(tmp) / "result"
        stage.mkdir()
        (stage / "report.json").write_bytes(shared.encode(report))
        (stage / "accounts.json").write_bytes(shared.encode(first))
        shared.write_csv(stage / "metrics.csv", [dict(case=k, **v) for k, v in metrics.items()])
        shared.write_csv(
            stage / "daily-nav.csv",
            [{"date": d, **{k: v["equity"][d]["nav"] for k, v in first.items()}} for d in calendar],
        )
        (stage / "manifest.json").write_bytes(
            shared.encode(
                {
                    "input_sha256": identity,
                    "outputs": {p.name: shared.digest(p) for p in stage.iterdir()},
                }
            )
        )
        stage.rename(target)
    return {**report, "output": str(target), "reused": False}


def comparison_deltas(branches):
    if any(branches[name]["state"] != "complete" for name in ("original", "approx")):
        return None
    result = {}
    for case in ("A0_base", "A0_base_slip"):
        a, b = (branches[name]["metrics"][case] for name in ("original", "approx"))
        result[case] = {
            key: b[key] - a[key] if a[key] is not None and b[key] is not None else None
            for key in ("net_profit", "return_pct", "drawdown_pct", "annual_return_pct", "pf")
        }
    return result


def run(project, lab=None):
    project = Path(project).resolve()
    root = project / "data/irbank_jsf"
    if not authorized(root):
        return {"state": "disabled"}
    lab = Path(lab).resolve() if lab else Path(str(project) + "トレード手法検討バックテスト用")
    if any("onedrive" in part.lower() for p in (project, lab) for part in p.parts):
        raise ValueError("local_non_onedrive_paths_required")
    with single_writer(root / "first5-comparison-lock"):
        technical, days, pools, states, acquisition = inputs(project, root)
        required_days = days[days.index(shared.ANCHOR) - 1 : days.index(shared.FIRST5_END)]
        branches = {
            "original": original_sources(project, required_days),
            "approx": {**acquisition, "metrics": None},
        }
        if any(v["state"] == "ready" for v in branches.values()):
            fixed = shared.read(root / "acquisition-policy.json")["first5_comparison"]
            if not fixed.get("frozen_engine_sha256"):
                raise ValueError("fixed_engine_pins_missing")
            for filename, expected in fixed["frozen_engine_sha256"].items():
                if shared.digest(Path(filename)) != expected:
                    raise ValueError("fixed_engine_changed")
            calendar, market_pins = shared.history(project)
            if calendar != days:
                raise ValueError("first5_market_calendar_changed")
            for relative, expected in technical["source_sha256"].items():
                if shared.digest(project / relative) != expected:
                    raise ValueError("first5_technical_source_changed")
            if (
                shared.digest(project / "scripts/swing_gap_atr/nojsf_decade_ranks.py")
                != technical["feature_source_sha256"]
            ):
                raise ValueError("first5_technical_implementation_changed")
            with shared.imports(project, lab) as modules:
                for name, branch in list(branches.items()):
                    if branch["state"] != "ready":
                        continue
                    try:
                        ranks, credit = (
                            approximate_ranks(root, days, pools, states)
                            if name == "approx"
                            else original_ranks(project, modules, days, pools)
                        )
                        evidence = {
                            "market": market_pins,
                            "technical": shared.digest(root / "technical-candidates.json"),
                            "credit": credit,
                        }
                        branches[name] = replay_branch(
                            project, lab, modules, days, ranks, name, evidence
                        )
                    except (Halt, ValueError, KeyError, TypeError, OSError) as exc:
                        branches[name] = {
                            "state": "needs_attention",
                            "reason": type(exc).__name__ + ":" + str(exc)[:180],
                            "metrics": None,
                        }

        report = {
            "state": "complete"
            if all(x["state"] == "complete" for x in branches.values())
            else "needs_attention"
            if any(x["state"] == "needs_attention" for x in branches.values())
            else "waiting",
            "checked_at": now().isoformat(),
            "spec": SPEC,
            "acquisition": acquisition,
            "branches": branches,
            "deltas": comparison_deltas(branches),
        }
        save(root / "first5-comparison-status.json", report)
        return report


def safe_check(project):
    try:
        result = run(project)
        return {k: result[k] for k in ("state", "acquisition") if k in result} | {
            "branches": {
                k: {field: v.get(field) for field in ("state", "reason", "output", "reused")}
                for k, v in result.get("branches", {}).items()
            }
        }
    except (Halt, ValueError, KeyError, TypeError, OSError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "broker_state_already_open":
            return {"state": "already_running"}
        result = {"state": "needs_attention", "reason": type(exc).__name__ + ":" + str(exc)[:180]}
        save(Path(project) / "data/irbank_jsf/first5-comparison-error.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args(argv)
    result = safe_check(args.project)
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result["state"] == "needs_attention" else 0


if __name__ == "__main__":
    raise SystemExit(main())
