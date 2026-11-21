"""Reconcile prespecified IRBANK names against archived official JSF balances."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from trading_system.research.irbank_jsf import Archive, Halt, save
from trading_system.research.irbank_universe import prepare, sha
from trading_system.research.swing_credit import read_credit, tokyo

FIELDS = ("loan_balance", "stock_loan_balance", "net_balance")
OFFICIAL = ("融資残高（株）", "貸株残高（株）", "差引残高（株）")


def official(project, day):
    base = project / "data/free-market/jsf-history"
    path = base / "days" / (day + ".json")
    if path.exists():
        meta = json.loads(path.read_bytes())
        if meta["state"] != "checked" or meta["date"] != day:
            raise Halt("official_manifest_unchecked")
        for key in ("csv", "html"):
            if sha(base / meta[key]["path"]) != meta[key]["sha256"]:
                raise Halt("official_raw_hash_mismatch")
        return tokyo((base / meta["csv"]["path"]).read_bytes()), meta["csv"]["sha256"]
    # The existing study froze its November refresh separately from the daily paper captures.
    study = project / "data/offline-verification/swing-decade-capitals-20261118"
    snapshot = study / "free-jsf" if day <= "2026-11-17" else project
    meta_path = snapshot / "data/swing-forward/credit" / day / "manifest.json"
    meta = json.loads(meta_path.read_bytes())
    if day <= "2026-11-17":
        frozen = json.loads((study / "refreshed/sources.json").read_bytes())
        if meta["hashes"] != frozen[day]["hashes"]:
            raise Halt("official_frozen_snapshot_changed")
    # This is a retrospective numerical audit; receipt time is not an assumed publication time.
    cutoff = datetime.fromisoformat(meta["available_at"])
    rows, meta = read_credit(snapshot, day, cutoff)
    return rows, meta["hashes"]["all.csv"]


def compare(irbank, official_days):
    dates = sorted(official_days)
    positions = {d: i for i, d in enumerate(dates)}
    shifts = {str(s): Counter() for s in range(-2, 3)}
    names = {}
    mismatches = []
    field_counts = {k: Counter() for k in FIELDS}
    for code, rows in irbank.items():
        count = Counter(observed=len(rows))
        for day, row in rows.items():
            if day not in positions:
                count["official_day_missing"] += 1
                continue
            values = tuple(row[k] for k in FIELDS)
            if code + "0" not in official_days[day]:
                count["official_code_missing"] += 1
            for shift in range(-2, 3):
                i = positions[day] + shift
                if not 0 <= i < len(dates):
                    continue
                entry = official_days[dates[i]].get(code + "0")
                if not entry:
                    continue
                try:
                    expected = tuple(int(entry[k].replace(",", "")) for k in OFFICIAL)
                except (ValueError, KeyError, AttributeError, TypeError):
                    if shift == 0:
                        count["official_values_invalid"] += 1
                    continue
                shifts[str(shift)]["compared"] += 1
                shifts[str(shift)]["matched"] += int(expected == values)
                if shift == 0:
                    count["compared"] += 1
                    count["matched"] += int(expected == values)
                    for k, a, b in zip(FIELDS, values, expected, strict=True):
                        field_counts[k]["compared"] += 1
                        field_counts[k]["matched"] += int(a == b)
                    if expected != values and len(mismatches) < 100:
                        mismatches.append(
                            {"code": code, "date": day, "irbank": values, "official": expected}
                        )
        count["uncompared"] = count["observed"] - count["compared"]
        names[code] = dict(count)
    return {
        "by_code": names,
        "offset_sessions": {k: dict(v) for k, v in shifts.items()},
        "offset_basis": "Adjacent available official dates; missing dates are reported separately",
        "fields": {k: dict(v) for k, v in field_counts.items()},
        "mismatch_examples": mismatches,
    }


def run(project, root):
    project, root = Path(project), Path(root)
    u = prepare(project, root)
    archive = Archive(root)
    data = {}
    raw_hashes = {}
    pending = []
    for code in u["audit_codes"]:
        state_path = root / "state/audit" / (code + ".json")
        if not state_path.exists():
            pending.append(code)
            continue
        state = json.loads(state_path.read_bytes())
        if state.get("state") != "complete":
            pending.append(code)
            continue
        days = {}
        for end in state["pages"]:
            receipt, rows = archive.read(code, end)
            raw_hashes[code + "/" + end] = receipt["sha256"]
            for row in rows:
                if u["audit_start"] <= row["date"] <= u["audit_end"]:
                    old = days.get(row["date"])
                    if old is not None and old != row:
                        raise Halt("audit_overlap_conflict")
                    days[row["date"]] = row
        data[code] = days
    if pending:
        result = {"state": "waiting", "completed_codes": len(data), "pending_codes": pending}
        save(root / "audit-report.json", result)
        return result
    days = sorted({d for rows in data.values() for d in rows})
    refs, frames, missing = {}, {}, []
    for day in days:
        try:
            frame, digest = official(project, day)
        except (FileNotFoundError, ValueError) as exc:
            missing.append({"date": day, "reason": type(exc).__name__})
            continue
        refs[day] = digest
        frames[day] = frame
    result = compare(data, frames)
    zero = result["offset_sessions"]["0"]
    checked = sum(v.get("compared", 0) > 0 for v in result["by_code"].values())
    result.update(
        state="checked"
        if checked == 100
        and zero.get("compared", 0) > 0
        and zero.get("matched") == zero.get("compared")
        and not missing
        and all(v["uncompared"] == 0 for v in result["by_code"].values())
        else "needs_review",
        sampled_codes=len(data),
        compared_codes=checked,
        application_date_basis=True,
        raw_sha256=raw_hashes,
        official_csv_sha256=refs,
        official_missing=missing,
        publication_time_verified=False,
        restriction_field_available=False,
        notes=[
            "Final historical responses do not prove point-in-time availability.",
            "IRBANK updates the previous session in the evening; "
            "newer official rows take priority.",
        ],
    )
    save(root / "audit-report.json", result)
    return result
