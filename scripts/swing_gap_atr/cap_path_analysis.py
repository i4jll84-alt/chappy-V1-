"""Decompose continuous swing-account results by calendar year and by matched entries."""

import argparse
import csv
from collections import defaultdict
from decimal import Decimal
from itertools import combinations, groupby
from pathlib import Path

D = Decimal


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def write(path, data):
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(data[0]))
        writer.writeheader()
        writer.writerows(data)


def summarize(results, out):
    daily = rows(results / "nav.csv")
    closed = rows(results / "trades.csv")
    accounts = defaultdict(list)
    for r in daily:
        if r["scope"] == "history":
            accounts[r["policy"], r["cap"]].append(r)
    trades = defaultdict(dict)
    for r in closed:
        if r["scope"] != "history":
            continue
        ledger = trades[r["policy"], r["cap"]]
        key = r["entry_day"], r["code"]
        if key in ledger:
            raise ValueError(f"Repeated opening lot {key}")
        ledger[key] = D(r["net"])
    yearly = []
    for (policy, cap), ledger in accounts.items():
        dates = [r["date"] for r in ledger]
        assert dates == sorted(set(dates)) and D(ledger[0]["market_value"]) == 0
        previous = D(ledger[0]["nav"])
        for year, group in groupby(ledger[1:], key=lambda r: r["date"][:4]):
            marks = list(group)
            peak = previous
            dd = D(0)
            for m in marks:
                nav = D(m["nav"])
                assert abs(nav - D(m["cash"]) - D(m["market_value"])) < D("1e-15")
                peak = max(peak, nav)
                dd = min(dd, nav / peak - 1)
            yearly.append(
                dict(
                    policy=policy,
                    cap=cap,
                    year=year,
                    sessions=len(marks),
                    opening_asset=previous,
                    closing_asset=nav,
                    pnl=nav - previous,
                    return_pct=100 * (nav / previous - 1),
                    maximum_drawdown_pct=100 * dd,
                )
            )
            previous = nav
    decompositions = []
    for policy in dict.fromkeys(k[0] for k in accounts):
        for a, b in combinations([c for p, c in accounts if p == policy], 2):
            x, y = trades[policy, a], trades[policy, b]
            common = x.keys() & y.keys()
            shared = sum((y[k] - x[k] for k in sorted(common)), D(0))
            if abs(shared) > D("1e-12") or any(
                abs(y[k] - x[k]) > D("1e-12") for k in sorted(common)
            ):
                raise ValueError("Same opening lot sold for a different amount")
            xa = sum((x[k] for k in sorted(x.keys() - y.keys())), D(0))
            yb = sum((y[k] for k in sorted(y.keys() - x.keys())), D(0))
            end_a, end_b = accounts[policy, a][-1], accounts[policy, b][-1]
            open_delta = D(end_b["unrealized"]) - D(end_a["unrealized"])
            difference = D(end_b["nav"]) - D(end_a["nav"])
            assert abs(difference - (yb - xa + shared + open_delta)) < D("1e-15")
            decompositions.append(
                dict(
                    policy=policy,
                    first_cap=a,
                    second_cap=b,
                    common_closed=len(common),
                    first_only_closed=len(x.keys() - y.keys()),
                    first_only_realized=xa,
                    second_only_closed=len(y.keys() - x.keys()),
                    second_only_realized=yb,
                    common_closed_difference=shared,
                    terminal_unrealized_difference=open_delta,
                    nav_difference=difference,
                )
            )
    out.mkdir(parents=True, exist_ok=True)
    full = [
        r
        for r in rows(results / "summary.csv")
        if (r["scope"], r["period"], r["policy"]) == ("history", "full", "morning")
    ]
    fields = (
        "cap",
        "nav",
        "net",
        "total_return_pct",
        "cagr_pct",
        "cagr_intervals_pct",
        "max_dd_pct",
        "calmar",
        "sharpe",
        "mean_positions",
        "max_positions",
        "avg_utilization_pct",
        "buys",
        "sells",
        "cap_skips",
        "cap_only",
        "fallback_exits",
    )
    write(out / "cap_comparison.csv", [{f: r[f] for f in fields} for r in full])
    write(out / "cap_by_year.csv", yearly)
    write(out / "cap_trade_difference.csv", decompositions)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    summarize(args.results, args.out)
