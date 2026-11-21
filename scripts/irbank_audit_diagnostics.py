"""Explain audit differences without approving a change to A0's source definition."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from trading_system.research.irbank_audit import FIELDS, OFFICIAL
from trading_system.research.irbank_jsf import Archive, Halt, save
from trading_system.research.irbank_universe import sha
from trading_system.research.swing_credit import records


def balances(body):
    grouped = defaultdict(list)
    for row in records(body):
        try:
            values = tuple(int(row[k].replace(",", "")) for k in OFFICIAL)
        except (ValueError, KeyError):
            values = None
        grouped[row["銘柄コード"]].append((row["市場区分"], values))
    result = {}
    for code, rows in grouped.items():
        tse = next((v for market, v in rows if market == "東証"), None)
        total = (
            tuple(sum(v[i] for _, v in rows) for i in range(3))
            if all(v is not None for _, v in rows)
            else None
        )
        result[code] = (tse, total)
    return result


def main():
    project = Path(__file__).resolve().parents[1]
    root = project / "data/irbank_jsf"
    audit = json.loads((root / "audit-report.json").read_bytes())
    values = defaultdict(dict)
    archive = Archive(root)
    for key in audit["raw_sha256"]:
        code, end = key.split("/")
        receipt, rows = archive.read(code, end)
        if receipt["sha256"] != audit["raw_sha256"][key]:
            raise Halt("audit_source_changed")
        for row in rows:
            values[row["date"]][code] = tuple(row[k] for k in FIELDS)
    counts, examples, pins = Counter(), [], {}
    by_date = {}
    for day, digest in audit["official_csv_sha256"].items():
        base = project / "data/free-market/jsf-history"
        meta = base / "days" / (day + ".json")
        if meta.exists():
            csv_path = base / json.loads(meta.read_bytes())["csv"]["path"]
        else:
            snapshot = (
                project / "data/offline-verification/swing-decade-capitals-20261118/free-jsf"
                if day <= "2026-11-17"
                else project
            )
            csv_path = snapshot / "data/swing-forward/credit" / day / "all.csv"
        if sha(csv_path) != digest:
            raise Halt("official_original_changed")
        original = balances(csv_path.read_bytes())
        fresh = {}
        folder = root / "diagnostics" / ("jsf-" + day.replace("-", ""))
        if (folder / "receipt.json").exists():
            receipt = json.loads((folder / "receipt.json").read_bytes())
            if receipt["date"] != day or sha(folder / "all.csv") != receipt["sha256"]:
                raise Halt("fresh_official_changed")
            fresh = balances((folder / "all.csv").read_bytes())
            pins[day] = receipt["sha256"]
        day_counts = Counter()
        for code, value in values[day].items():
            tse, total = original.get(code, (None, None))
            current_tse, current_total = fresh.get(code, (None, None))
            if tse is None:
                continue
            if value == tse:
                reason = "original_tse_match"
            elif value == total:
                reason = "multiple_markets_sum"
            elif value == current_tse:
                reason = "later_official_version"
            elif value == current_total:
                reason = "markets_sum_and_later_official"
            else:
                reason = "unexplained"
            counts[reason] += 1
            day_counts[reason] += 1
            if reason != "original_tse_match" and len(examples) < 50:
                examples.append(
                    dict(
                        date=day,
                        code=code,
                        reason=reason,
                        irbank=value,
                        original_tse=tse,
                        original_all_markets=total,
                        later_tse=current_tse,
                        later_all_markets=current_total,
                    )
                )
        by_date[day] = dict(day_counts)
    result = {
        "state": "research_definition_review_required",
        "counts": dict(counts),
        "by_date": by_date,
        "examples": examples,
        "fresh_official_sha256": pins,
        "audit_sha256": sha(root / "audit-report.json"),
        "notes": [
            "This diagnostic does not change the strict audit gate or historical inputs.",
            "exchange=XTKS selects reverse-fee grouping; "
            "tested days balances include other markets.",
            "Later official values do not demonstrate availability at the original decision time.",
        ],
    }
    save(root / "audit-diagnostics.json", result)
    print(json.dumps({"state": result["state"], "counts": result["counts"]}))


if __name__ == "__main__":
    main()
