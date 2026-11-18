"""Reconcile the change in 6-session net P&L by candidate with V1's original frozen fills."""

import argparse
import csv
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path


def rows(source):
    with source.open(encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def reconcile(project, results):
    paired = {
        r["date"]: Decimal(r["guarded_minus_plus3"])
        for r in rows(results / "paired.csv")
        if all(r[f"{s}_status"] == "ok" for s in ("current", "plus3", "guarded"))
    }
    reference_file = (
        project / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs/trades.csv"
    )
    original = {
        (r["decision"], r["code"]): Decimal(r["net"])
        for r in rows(reference_file)
        if r["decision"] in paired and r["arm"] == "new" and r["capital"] == "2000000"
    }
    by_date = defaultdict(Decimal)
    by_reason = defaultdict(Decimal)
    occurrences = Counter()
    total_favorable = defaultdict(Decimal)
    total_unfavorable = defaultdict(Decimal)
    original_matched = set()
    for r in rows(results / "orders.csv"):
        if r["date"] not in paired or r["rule"] != "guarded":
            continue
        key = r["date"], r["code"]
        baseline_net = original.get(key, Decimal(0))
        if key in original:
            original_matched.add(key)
        net = Decimal(r["net"] or "0")
        reason = r["exit_type"]
        delta = net - baseline_net
        if reason == "scheduled":
            assert delta == 0, key
        if reason == "unresolved":
            raise ValueError(f"Unresolved order in common window {key}")
        by_date[r["date"]] += delta
        by_reason[reason] += delta
        occurrences[reason] += 1
        total_favorable[reason] += max(delta, 0)
        total_unfavorable[reason] += min(delta, 0)
    assert original_matched == set(original)
    assert all(abs(by_date[k] - expected) < Decimal("0.000001") for k, expected in paired.items())
    report = {
        "comparable_windows": len(paired),
        "confirmed_original_fills": len(original),
        "reason": {
            reason: {
                "count": occurrences[reason],
                "average_change_jpy_per_window": str(amount / len(paired)),
                "favorable_changes_total_jpy": str(total_favorable[reason]),
                "unfavorable_changes_total_jpy": str(total_unfavorable[reason]),
            }
            for reason, amount in by_reason.items()
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--episodes", required=True, type=Path)
    args = parser.parse_args()
    reconcile(args.project, args.episodes)
