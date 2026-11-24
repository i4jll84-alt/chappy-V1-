"""Acquire every frozen candidate's required dates before filling unused history gaps."""

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from trading_system.brokers.locking import single_writer
from trading_system.research.irbank_jsf import Archive, Client, Halt, load_key, now, save
from trading_system.research.irbank_required import prepare_candidates
from trading_system.research.irbank_universe import prepare, sha

MODE = "all_fixed_candidate_dates_v1"
ENDS = {"first5": "2021-11-19", "history": "2023-10-31"}
LAST_SIGNALS = {"first5": "2021-11-18", "history": "2023-10-31"}


def target_dates(technical, scope="first5"):
    days = technical["sessions"]
    index = {d: i for i, d in enumerate(days)}
    if days != sorted(set(days)) or scope not in ENDS:
        raise Halt("priority_calendar_invalid")
    needs = defaultdict(set)
    for day, pool in technical["days"].items():
        if not "2016-11-18" <= day <= LAST_SIGNALS[scope]:
            continue
        if day not in index or index[day] == 0:
            raise Halt("priority_previous_session_missing")
        for item in pool:
            code = item["code"][:-1]
            needs[code].update((days[index[day] - 1], day))
    return {code: sorted(dates) for code, dates in needs.items()}


def signature(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def enabled(root):
    path = Path(root) / "acquisition-policy.json"
    if not path.exists():
        return False
    policy = json.loads(path.read_bytes())
    return policy.get("priority_plan", {}).get("mode") == MODE


def check_plan(root, technical):
    policy = json.loads((Path(root) / "acquisition-policy.json").read_bytes())["priority_plan"]
    if (
        policy.get("mode") != MODE
        or policy.get("technical_sha256") != sha(Path(root) / "technical-candidates.json")
        or policy.get("first5_targets_sha256") != signature(target_dates(technical))
        or policy.get("history_targets_sha256") != signature(target_dates(technical, "history"))
    ):
        raise Halt("priority_plan_changed")


def coverage(archive, code, dates):
    """An absent row remains unobserved, even when the requested range is exhausted."""
    values, intervals, pages = {}, [], []
    for path in sorted((archive.root / "raw" / code).glob("*/receipt.json")):
        receipt, rows = archive.read(code, path.parent.name)
        pages.append(receipt["to"])
        # A 404 before a given end provides no historical rows through that end.
        # It certifies an attempted scope, never a zero balance or a passing signal.
        intervals.append((rows[0]["date"] if rows else "0001-01-01", receipt["to"]))
        for row in rows:
            day = row["date"]
            if day in values and values[day] != row:
                raise Halt("required_overlap_conflict")
            values[day] = row
    missing = [d for d in sorted(dates) if not any(a <= d <= b for a, b in intervals)]
    return {
        "code": code,
        "coverage_mode": MODE,
        "required_dates": sorted(dates),
        "pages": pages,
        "state": "pending" if missing else "complete",
        "unrequested_dates": missing,
        "unobserved_dates": [d for d in sorted(dates) if d not in missing and d not in values],
        "observed_dates": sum(d in values for d in dates),
    }


def ready_states(root, technical, scope="first5"):
    check_plan(root, technical)
    archive = Archive(root)
    needs = target_dates(technical, scope)
    states, missing = {}, []
    requested = observed = unobserved = 0
    for code, dates in needs.items():
        result = coverage(archive, code, dates)
        observed += result["observed_dates"]
        unobserved += len(result["unobserved_dates"])
        requested += len(dates) - len(result["unrequested_dates"])
        if result["state"] == "complete":
            states[code] = result
        else:
            missing.append(code)
    return states, {
        "state": "waiting" if missing else "ready",
        "coverage_scope": MODE,
        "target_codes": len(needs),
        "complete_codes": len(states),
        "remaining_codes": len(missing),
        "first_missing": missing[:10],
        "required_dates": sum(map(len, needs.values())),
        "requested_dates": requested,
        "observed_dates": observed,
        "unobserved_dates": unobserved,
    }


def run(project, root, max_requests=950, scope="first5"):
    project, root = Path(project), Path(root)
    technical = prepare_candidates(project, root)
    check_plan(root, technical)
    universe = prepare(project, root)
    needs = target_dates(technical, scope)
    if set(needs) - set(universe["codes"] + universe["boundary_only_codes"]):
        raise Halt("priority_universe_mismatch")
    archive = Archive(root)
    days = technical["sessions"]
    index = {d: i for i, d in enumerate(days)}
    report = {
        "phase": scope + "_required",
        "started_at": now().isoformat(),
        "jobs": len(needs),
        "complete": 0,
        "empty": 0,
        "errors": 0,
        "requests_this_run": 0,
        "state": "running",
    }
    with single_writer(root / "dispatch"):
        client = Client(root, load_key(project), max_requests)
        try:
            for code, dates in needs.items():
                state = coverage(archive, code, dates)
                while state["unrequested_dates"]:
                    first = state["unrequested_dates"][0]
                    end = days[min(index[first] + 251, index[ENDS[scope]])]
                    if archive.read(code, end) is not None:
                        end = first
                    if archive.read(code, end) is not None:
                        raise Halt("priority_date_unresolved")
                    receipt, rows = archive.write(code, end, client.get(code, end))
                    archive.merge(code, receipt, rows)
                    next_state = coverage(archive, code, dates)
                    if (
                        next_state["unrequested_dates"] == state["unrequested_dates"]
                        and end == first
                    ):
                        raise Halt("priority_date_unresolved")
                    state = next_state
                    save(root / "state" / (scope + "-required") / (code + ".json"), state)
                save(root / "state" / (scope + "-required") / (code + ".json"), state)
                report["complete"] += 1
                report["empty"] += int(state["observed_dates"] == 0)
                report.update(
                    requests_this_run=client.sent,
                    remaining=len(needs) - report["complete"],
                    last={"phase": scope + "_required", "code": code},
                )
                save(root / "priority-progress.json", report)
                print(json.dumps(report), flush=True)
            report["state"] = "complete"
        except Halt as exc:
            report.update(state="stopped", reason=str(exc))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report.update(state="failed", reason="validation_or_storage_" + type(exc).__name__)
            report["errors"] += 1
        finally:
            report.update(
                requests_this_run=client.sent,
                remaining=len(needs) - report["complete"],
                finished_at=now().isoformat(),
            )
            save(root / "priority-progress.json", report)
            client.close()
    return report
