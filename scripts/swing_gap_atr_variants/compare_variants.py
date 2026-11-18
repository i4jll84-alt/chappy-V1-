"""Summarize matched starts for the three variants using the frozen 730-date calendar."""

import argparse
import csv
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from statistics import mean, median

D = Decimal
PRIMARY = ("guarded", "no_jsf", "extend_ma5", "limit_gu3")
ALL = (*PRIMARY, "extend_prior")


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as src:
        return list(csv.DictReader(src))


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as dst:
        writer = csv.DictWriter(dst, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def indexed(rows, forced_rule=None):
    result = {}
    for row in rows:
        key = row["date"], forced_rule or row["rule"]
        if key in result:
            raise ValueError(f"Duplicate result: {key}")
        if row["status"] == "ok":
            net, drawdown = D(row["net"]), D(row["dd_pct"])
            if not net.is_finite() or not 0 <= drawdown <= 100:
                raise ValueError(f"Invalid completed account: {key}")
        else:
            net = drawdown = None
        result[key] = dict(status=row["status"], net=net, dd=drawdown, issues=row.get("issues", ""))
    return result


def compare(results, dates, arm):
    selected = [d for d in dates if all(results[d, r]["status"] == "ok" for r in ("guarded", arm))]
    if not selected:
        return dict(n=0)
    nets = [results[d, arm]["net"] for d in selected]
    baseline = [results[d, "guarded"]["net"] for d in selected]
    dd = [results[d, arm]["dd"] for d in selected]
    delta = [a - b for a, b in zip(nets, baseline, strict=True)]
    return dict(
        n=len(selected),
        mean=mean(nets),
        baseline_mean=mean(baseline),
        delta=mean(delta),
        median=median(nets),
        worst=min(nets),
        positive=sum(x > 0 for x in nets),
        mean_mdd_pct=mean(dd),
        better=sum(x > 0 for x in delta),
        same=delta.count(0),
        worse=sum(x < 0 for x in delta),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--paths", type=Path, required=True, help="Daily-window CSV for guarded / extend / limit"
    )
    parser.add_argument(
        "--no-jsf", type=Path, required=True, help="Daily-window CSV for the no-JSF arm"
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    prior = args.project / "data/offline-verification/swing-gap-atr-20261118/episodes"
    calendar = read_csv(prior / "paired.csv")
    slot = {r["date"]: i for i, r in enumerate(calendar)}
    assert len(calendar) == len(slot) == 730 and list(slot) == sorted(slot)
    dates = [
        r["date"]
        for r in calendar
        if all(r[a + "_status"] == "ok" for a in ("current", "plus3", "guarded"))
    ]
    assert len(dates) == 709
    results = indexed(read_csv(args.paths))
    results.update(indexed(read_csv(args.no_jsf), forced_rule="no_jsf"))
    old = indexed([r for r in read_csv(prior / "windows.csv") if r["rule"] == "guarded"])
    target = set(dates)
    for arm in ALL:
        if {d for d, r in results if r == arm} != target:
            raise ValueError(f"Eligible start coverage mismatch for {arm}")
    for d in dates:
        x, y = old[d, "guarded"], results[d, "guarded"]
        for key in ("net", "dd"):
            assert abs(x[key] - y[key]) < D("1e-15"), (d, key, x, y)
    common = [d for d in dates if all(results[d, r]["status"] == "ok" for r in PRIMARY)]
    comparison = {r: compare(results, common, r) for r in PRIMARY}
    pairwise = {r: compare(results, dates, r) for r in ALL}
    years = {
        y: {r: compare(results, [d for d in common if d.startswith(y)], r) for r in PRIMARY}
        for y in sorted({d[:4] for d in dates})
    }
    # Anchor to the complete exchange-calendar decision sequence, before exclusions.
    offsets = {
        str(i): {r: compare(results, [d for d in common if slot[d] % 20 == i], r) for r in PRIMARY}
        for i in range(20)
    }
    assert sum(offsets[str(i)]["guarded"]["n"] for i in range(20)) == len(common)
    both = [
        d
        for d in dates
        if all(results[d, r]["status"] == "ok" for r in ("guarded", "extend_ma5", "extend_prior"))
    ]
    sensitivity = {r: compare(results, both, r) for r in ("guarded", "extend_ma5", "extend_prior")}
    issue_days = {}
    for arm in ALL:
        breakdown = Counter(results[d, arm]["status"] for d in dates)
        issue_days[arm] = dict(breakdown)
    records = []
    for d in dates:
        row = {"date": d, "calendar_index": slot[d], "all_four_resolved": d in common}
        for r in ALL:
            result = results[d, r]
            for key in ("status", "net", "dd", "issues"):
                row[f"{r}_{key}"] = result[key]
        records.append(row)
    summary = dict(
        planned=len(dates),
        common=len(common),
        date_range=[dates[0], dates[-1]],
        coverage=issue_days,
        primary=comparison,
        pairs=pairwise,
        years=years,
        offsets_20_sessions=offsets,
        extension_same_dates=sensitivity,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "comparison.csv", records)
    (args.out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf8"
    )


if __name__ == "__main__":
    main()
