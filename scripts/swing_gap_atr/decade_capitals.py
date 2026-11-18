"""Compare the three capital accounts and value the explicit ten-year cash-wait scenario."""

import argparse
import csv
import json
from collections import Counter
from decimal import Decimal
from itertools import groupby
from pathlib import Path

D = Decimal
CAPITALS = (1_000_000, 2_000_000, 3_000_000)


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def write(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=records[0])
        writer.writeheader()
        writer.writerows(records)


def history(records):
    return [
        r for r in records if (r["scope"], r["policy"], r["cap"]) == ("history", "morning", "none")
    ]


def facts(record):
    return {k: v for k, v in record.items() if v != ""}


def compare(project, coverage, accounts, output):
    output.mkdir(parents=True, exist_ok=True)
    window = rows(coverage / "decade_coverage_days.csv")
    first, last = window[0]["decision"], window[-1]["next_session"]
    assert (first, last) == ("2016-11-18", "2026-11-18")
    assert all(a["next_session"] == b["decision"] for a, b in zip(window, window[1:]))
    monitored = [r for r in window if r["input_status"] != "missing_history"]
    assert monitored and window[-len(monitored) :] == monitored
    assert all(r["input_status"] == "missing_history" for r in window[: -len(monitored)])
    supported_start = monitored[0]["decision"]
    past = project / "data/offline-verification/swing-position-caps-20261118"
    comparison, scenario, yearly, reconciliation = [], [], [], {}
    for capital in CAPITALS:
        folder = accounts / str(capital)
        info = json.loads((folder / "inputs.json").read_bytes())
        assert info["initial_capital"] == capital
        account = history(rows(folder / "nav.csv"))
        expected_dates = [supported_start, *(r["next_session"] for r in monitored)]
        assert [r["date"] for r in account] == expected_dates
        assert D(account[0]["nav"]) == D(account[0]["cash"]) == capital
        assert int(account[0]["pos_count"]) == 0
        signals = rows(folder / "signals.csv")
        assert [(r["decision"], r["entry"], r["candidates"]) for r in signals] == [
            (r["decision"], r["next_session"], r["frozen_candidates"]) for r in monitored
        ]
        if capital == 2_000_000:
            for filename in ("nav", "events", "trades"):
                old = history(rows(past / (filename + ".csv")))
                new = history(rows(folder / (filename + ".csv")))
                assert list(map(facts, old)) == list(map(facts, new)), filename
                reconciliation[filename] = len(new)

        (primary,) = [r for r in history(rows(folder / "summary.csv")) if r["period"] == "full"]
        counts = Counter(r["input_status"] for r in window)
        measured = iter(account[1:])

        def daily(day, status, decision, asset, cash, count):
            return dict(
                initial_capital=capital,
                date=day,
                decision=decision,
                input_status=status,
                cash_wait_scenario_nav=asset,
                cash=cash,
                positions=count,
            )

        path = [daily(first, "inception", "", D(capital), D(capital), 0)]
        for day in window:
            status = day["input_status"]
            if status == "missing_history":
                # This scenario starts flat and the archival gap is a single prefix.
                assert path[-1]["positions"] == 0 and path[-1]["cash"] == capital
                item = daily(
                    day["next_session"], status, day["decision"], D(capital), D(capital), 0
                )
            else:
                tick = next(measured)
                assert tick["date"] == day["next_session"]
                item = daily(
                    tick["date"],
                    status,
                    day["decision"],
                    D(tick["nav"]),
                    D(tick["cash"]),
                    int(tick["pos_count"]),
                )
            path.append(item)
        assert next(measured, None) is None
        peak = D(capital)
        drawdown = D(0)
        for tick in path:
            value = tick["cash_wait_scenario_nav"]
            peak = max(peak, value)
            drawdown = min(drawdown, value / peak - 1)
        assert abs(drawdown * 100 - D(primary["max_dd_pct"])) < D("1e-15")
        assert path[-1]["cash_wait_scenario_nav"] == D(primary["nav"])
        scenario.extend(path)
        comparison.append(
            dict(
                initial_capital=capital,
                supported_start=supported_start,
                end=last,
                supported_sessions=len(monitored),
                unsupported_decisions=counts["missing_history"],
                total_sessions=len(window),
                true_ten_year_pnl=None,
                true_ten_year_return_pct=None,
                end_nav=primary["nav"],
                net=primary["net"],
                total_return_pct=primary["total_return_pct"],
                supported_max_drawdown_pct=primary["max_dd_pct"],
                supported_sharpe=primary["sharpe"],
                supported_avg_invested_pct=primary["avg_utilization_pct"],
                buys=primary["buys"],
                sells=primary["sells"],
                end_positions=primary["open_positions"],
                model_exits=primary["fallback_exits"],
                cash_wait_scenario_net=path[-1]["cash_wait_scenario_nav"] - capital,
                cash_wait_scenario_10_calendar_year_cagr_pct=100
                * ((float(path[-1]["cash_wait_scenario_nav"] / capital)) ** 0.1 - 1),
            )
        )
        opening = D(capital)
        for year, bucket in groupby(account[1:], key=lambda r: r["date"][:4]):
            days = list(bucket)
            close = D(days[-1]["nav"])
            yearly.append(
                dict(
                    initial_capital=capital,
                    year=year,
                    first_return_day=days[0]["date"],
                    last_close=days[-1]["date"],
                    sessions=len(days),
                    opening_nav=opening,
                    closing_nav=close,
                    net=close - opening,
                    return_pct=100 * (close / opening - 1),
                )
            )
            opening = close
    write(output / "capital_comparison.csv", comparison)
    write(output / "capital_by_year.csv", yearly)
    write(output / "cash_wait_scenario_daily.csv", scenario)
    (output / "prior_2m_reconciliation.json").write_text(
        json.dumps(reconciliation, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("project", "coverage", "accounts", "out"):
        parser.add_argument("--" + key, required=True, type=Path)
    args = parser.parse_args()
    compare(args.project, args.coverage, args.accounts, args.out)
