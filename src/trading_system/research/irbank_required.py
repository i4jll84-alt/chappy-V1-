"""Fetch JSF pages in fixed candidate order, stopping only after five eligible names."""

import importlib.util
import json
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

import duckdb

from trading_system.brokers.locking import single_writer
from trading_system.research.irbank_jsf import Archive, Client, Halt, load_key, now, save
from trading_system.research.irbank_universe import prepare, sha
from trading_system.storage.store import atomic_write

FIRST = "2016-11-18"
LAST = "2023-10-31"
HALT_DAY = "2020-10-01"


def prepare_candidates(project, root):
    path = root / "technical-candidates.json"
    pin = path.with_suffix(".sha256")
    if path.exists():
        if not pin.exists() or sha(path) != pin.read_text().strip():
            raise Halt("technical_candidates_hash_mismatch")
        return json.loads(path.read_bytes())
    source = project / "scripts/swing_gap_atr/nojsf_decade_ranks.py"
    spec = importlib.util.spec_from_file_location("_irbank_fixed_features", source)
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    candidates = {}
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        dates, refs = owner.load_inputs(db, project)
        dates = [d for d in dates if d != HALT_DAY]
        db.execute("DROP TABLE sessions")
        db.execute(
            """CREATE TEMP TABLE sessions AS
            SELECT Date,row_number() OVER(ORDER BY Date) AS idx FROM bulk_calendar
            WHERE HolDiv IN ('1','2') AND Date<>?""",
            [HALT_DAY],
        )
        start, end = dates.index(FIRST), dates.index(LAST) + 1
        for left in range(start, end, 63):
            right = min(left + 63, end)
            rows = db.execute(
                owner.FEATURES, [dates[left - 20], dates[right - 1], dates[left], dates[right - 1]]
            ).fetchall()
            by_day = defaultdict(list)
            for day, code, close, old, _ in rows:
                by_day[day].append((-Fraction(close) / Fraction(old), code, str(close)))
            for day in dates[left:right]:
                candidates[day] = [
                    {"code": code, "close": close} for _, code, close in sorted(by_day[day])
                ]
    result = {
        "first": FIRST,
        "last": LAST,
        "sessions": dates,
        "days": candidates,
        "source_sha256": refs,
        "feature_source_sha256": sha(source),
        "selection": "All unchanged A0 technical criteria, then JSF, then the first five",
        "halt_day_excluded": HALT_DAY,
    }
    save(path, result)
    atomic_write(pin, sha(path).encode())
    return result


class Evidence:
    def __init__(self, archive, client, dates):
        self.archive, self.client, self.dates = archive, client, dates
        self.indices = {d: i for i, d in enumerate(dates)}
        self.memory = {}

    def read(self, code):
        if code in self.memory:
            return self.memory[code]
        rows, ranges, refs = {}, [], []
        for path in sorted((self.archive.root / "raw" / code).glob("*/receipt.json")):
            end = path.parent.name
            receipt, page = self.archive.read(code, end)
            refs.append(receipt["sha256"])
            for row in page:
                old = rows.get(row["date"])
                if old is not None and old != row:
                    raise Halt("required_overlap_conflict")
                rows[row["date"]] = row
            ranges.append((page[0]["date"] if page else end, end))
        if len(self.memory) >= 128:
            self.memory.pop(next(iter(self.memory)))
        self.memory[code] = (rows, ranges, refs)
        return self.memory[code]

    def pair(self, jqcode, previous, day):
        code = jqcode[:-1]
        for attempt in range(4):
            rows, ranges, refs = self.read(code)
            missing = [
                d
                for d in (previous, day)
                if d not in rows and not any(a <= d <= b for a, b in ranges)
            ]
            if not missing:
                return rows.get(previous), rows.get(day), refs
            if attempt == 3:
                break
            d = missing[0]
            upto = self.dates[min(self.indices[d] + 251, self.indices[LAST])]
            if attempt:
                upto = d
            cached = self.archive.read(code, upto)
            if cached is None:
                cached = self.archive.write(code, upto, self.client.get(code, upto))
            self.archive.merge(code, *cached)
            self.memory.pop(code, None)
        raise Halt("required_date_unresolved")


def decide(before, current):
    if before is None or current is None:
        return "no_data"
    values = [r[k] for r in (before, current) for k in ("loan_balance", "stock_loan_balance")]
    if max(values) > 0 and current["net_balance"] <= before["net_balance"]:
        return "pass"
    return "fail"


def reusable_decision(path, technical_sha, audit_sha, evidence):
    if not path.exists():
        return False
    pin = path.with_suffix(".sha256")
    if not pin.exists() or sha(path) != pin.read_text().strip():
        raise Halt("decision_hash_mismatch")
    old = json.loads(path.read_bytes())
    if not old["complete"] or old["technical_sha256"] != technical_sha:
        raise Halt("decision_input_mismatch")
    if old.get("audit_sha256") != audit_sha:
        raise Halt("decision_audit_changed")
    same = True
    for entry in old["tested"]:
        _, _, refs = evidence.read(entry["code"][:-1])
        if not set(entry["sources"]).issubset(refs):
            raise Halt("decision_raw_missing")
        same = same and set(entry["sources"]) == set(refs)
    return same


def run_needed(project, root, max_requests=950):
    project, root = Path(project), Path(root)
    audit_path = root / "audit-report.json"
    audit = json.loads(audit_path.read_bytes())
    audit_sha = sha(audit_path)
    if audit.get("state") != "checked":
        raise Halt("official_overlap_audit_not_passed")
    universe = prepare(project, root)
    technical = prepare_candidates(project, root)
    target = root / "required_decisions"
    target.mkdir(parents=True, exist_ok=True)
    dates = technical["sessions"]
    indices = {d: i for i, d in enumerate(dates)}
    allowed = {c + "0" for c in universe["codes"] + universe["boundary_only_codes"]}
    evidence_sha = sha(root / "technical-candidates.json")
    report = {
        "state": "running",
        "completed_days": 0,
        "total_days": len(technical["days"]),
        "started_at": now().isoformat(),
        "requests_this_run": 0,
    }
    with single_writer(root / "dispatch"):
        client = Client(root, load_key(project), max_requests)
        evidence = Evidence(Archive(root), client, dates)
        try:
            for day, pool in technical["days"].items():
                path = target / (day + ".json")
                pin = path.with_suffix(".sha256")
                if not reusable_decision(path, evidence_sha, audit_sha, evidence):
                    if path.exists():
                        version = target / "versions" / day / (sha(path) + ".json")
                        atomic_write(version, path.read_bytes())
                    previous = dates[indices[day] - 1]
                    selected, tested = [], []
                    for candidate in pool:
                        code = candidate["code"]
                        if code not in allowed:
                            raise Halt("candidate_outside_frozen_universe")
                        before, current, refs = evidence.pair(code, previous, day)
                        state = decide(before, current)
                        tested.append({"code": code, "state": state, "sources": refs})
                        if state == "pass":
                            selected.append(code)
                        if len(selected) == 5:
                            break
                    save(
                        path,
                        {
                            "date": day,
                            "previous": previous,
                            "selected": selected,
                            "tested": tested,
                            "complete": True,
                            "technical_sha256": evidence_sha,
                            "audit_sha256": audit_sha,
                            "restriction_assumption": "empty_pass_before_2023-11-01",
                            "pit_verified": False,
                            "recorded_at": now().isoformat(),
                        },
                    )
                    atomic_write(pin, sha(path).encode())
                report["completed_days"] += 1
                report["last_completed"] = day
                report["requests_this_run"] = client.sent
                save(root / "needed-progress.json", report)
                if report["completed_days"] % 25 == 0:
                    print(json.dumps(report, ensure_ascii=False), flush=True)
            report["state"] = "complete"
        except Halt as exc:
            report.update(state="stopped", reason=str(exc))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            report.update(state="failed", reason="validation_or_storage_" + type(exc).__name__)
        finally:
            report["requests_this_run"] = client.sent
            report["finished_at"] = now().isoformat()
            save(root / "needed-progress.json", report)
            client.close()
    return report
