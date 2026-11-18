"""Run fixed 100-share swing accounts with and without the JSF screen on identical schedules."""

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from unittest.mock import patch

CAPITALS = (1_000_000, 2_000_000, 3_000_000)
START, RECENT, END = "2016-11-18", "2023-10-31", "2026-11-18"


def matched_ranks(project, source):
    data = project / "data/offline-verification"
    original = data / "swing-decade-capitals-20261118"
    old = source.frozen_ranks(
        data / "swing-agent-replay-20261113/archive", data / "swing-periods-ma-jsf-20261113/outputs"
    )
    ranks = {d: r["plus3"] for d, r in old.items()}
    boundary = source.read(original / "boundary/boundary_proof.json")
    assert boundary["decision"] == RECENT and boundary["complete"] is True
    ranks[RECENT] = boundary["selected_codes"]
    for r in source.rows(original / "extended/ranks.csv"):
        assert r["decision"] not in ranks
        ranks[r["decision"]] = r["refreshed_codes"].split()
    return ranks, original / "extended"


def validate_reference(accounts, folder, source):
    counts = {}
    for name in ("nav", "events", "trades"):
        old = [r for r in source.rows(folder / (name + ".csv")) if r["scope"] == "extended_history"]

        def clean(r):
            return {k: str(v) for k, v in r.items() if k != "scope" and v not in (None, "")}

        new = [
            clean(dict(initial_capital=c, **r))
            for c in CAPITALS
            for r in getattr(accounts[c, "recent_jsf"], name)
        ]
        assert [clean(r) for r in old] == new, "JSF benchmark changed: " + name
        counts[name] = len(new)
    return counts


def run(project, ranks_dir, output, action_terms, board_lots):
    sys.path.insert(0, str(project / "src"))
    sys.path.insert(0, str(project / "scripts/swing_gap_atr"))
    import cap_backtest as engine
    import continuous as source
    from decade_market import DecadeMarket

    jsf, prior = matched_ranks(project, source)
    schedule = source.rows(ranks_dir / "signals.csv")
    selections = {r["decision"]: [] for r in schedule}
    references = {}
    for r in source.rows(ranks_dir / "candidates.csv"):
        d, code = r["decision"], r["code"]
        assert int(r["rank"]) == len(selections[d]) + 1
        selections[d].append(code)
        references[d, code] = (
            Decimal(r["prev_close"]),
            Decimal(r["atr14"]) if r["atr14"] else None,
        )
    codes = {c for arm in (jsf, selections) for seq in arm.values() for c in seq}
    market = DecadeMarket(project, codes)
    dates = market.sessions
    a, b = market.indices[START], market.indices[END]
    assert len(set(selections)) == 2440 and list(selections) == dates[a:b]
    assert sorted(jsf) == dates[market.indices[RECENT] : b] and len(jsf) == 742
    starts = {"decade_nojsf": START, "recent_nojsf": RECENT, "recent_jsf": RECENT}
    lots = None
    if board_lots:
        from board_lots import BoardLots

        lots = BoardLots(board_lots)
        starts["decade_confirmed_lots"] = START
    accounts = {
        (cap, scope): engine.Account("morning", None, scope, cash=Decimal(cap))
        for cap in CAPITALS
        for scope in starts
    }
    for (_, scope), acct in accounts.items():
        acct.snapshot(starts[scope], Decimal(0), 0, 0, 0)
    additions = {}
    if action_terms:
        for r in source.read(action_terms):
            assert r["notice_date"] < r["ex_date"] and r["url"].startswith("https://")
            additions[r["code"], r["ex_date"]] = (str(r["quantity_multiplier"]), r["url"])
    with patch.dict(engine.KNOWN, additions):
        for i, row in enumerate(schedule, a):
            day, entry, due = dates[i], dates[i + 1], dates[i + 7]
            assert (day, entry, due) == (row["decision"], row["entry"], row["due"])
            assert int(row["candidate_count"]) == len(selections[day]) <= 5
            picks = engine.reference_list(market, day, selections[day])
            for pick in picks:
                expected = references[day, pick["code"]]
                assert expected[0] == pick["prev_close"]
                if pick["atr"] is None or expected[1] is None:
                    assert expected[1] is pick["atr"]
                else:
                    assert abs(expected[1] - pick["atr"]) < Decimal("1e-10")
            other = engine.reference_list(market, day, jsf[day]) if day in jsf else []
            lot_picks = []
            if lots is not None:
                for pick in picks:
                    unit = lots.unit(pick["code"], entry)
                    if unit and 100 % unit == 0:
                        lot_picks.append(pick)
            bars = market.bars(entry)
            for (_, scope), acct in accounts.items():
                if day >= starts[scope]:
                    orders = picks
                    if scope == "recent_jsf":
                        orders = other
                    elif scope == "decade_confirmed_lots":
                        orders = lot_picks
                    engine.run_day(acct, entry, due, orders, bars, None)
    output.mkdir(exist_ok=True, parents=True)
    summary = []
    annual = []
    for (cap, scope), acct in accounts.items():
        stats = engine.metrics(acct, END, "full")
        elapsed = (date.fromisoformat(END) - date.fromisoformat(starts[scope])).days
        calendar_years = 10 if starts[scope] == START else elapsed / 365.2425
        peak = Decimal(cap)
        peak_day = starts[scope]
        max_dd = Decimal(0)
        dd_peak = dd_trough = peak_day
        for row in acct.nav:
            if row["nav"] >= peak:
                peak, peak_day = row["nav"], row["date"]
            dd = row["nav"] / peak - 1
            if dd < max_dd:
                max_dd, dd_peak, dd_trough = dd, peak_day, row["date"]
        assert max_dd * 100 == stats["max_dd_pct"]
        summary.append(
            dict(
                initial_capital=cap,
                **stats,
                calendar_cagr_pct=100 * ((float(stats["nav"]) / cap) ** (1 / calendar_years) - 1),
                drawdown_peak_date=dd_peak,
                drawdown_trough_date=dd_trough,
            )
        )
        prev = Decimal(cap)
        for year, records in groupby(acct.nav[1:], key=lambda r: r["date"][:4]):
            block = list(records)
            end = block[-1]["nav"]
            annual.append(
                dict(
                    initial_capital=cap,
                    scope=scope,
                    year=year,
                    sessions=len(block),
                    net=end - prev,
                    return_pct=100 * (end / prev - 1),
                    end_nav=end,
                )
            )
            prev = end
    source.write_csv(output / "summary.csv", summary)
    source.write_csv(output / "yearly.csv", annual)
    if lots is not None:
        exceptions = []
        for cap in CAPITALS:
            for event in accounts[cap, "decade_nojsf"].events:
                if event["action"] != "BUY":
                    continue
                unit = lots.unit(event["code"], event["date"])
                if unit is None or 100 % unit != 0:
                    exceptions.append(
                        dict(
                            initial_capital=cap,
                            date=event["date"],
                            code=event["code"],
                            price=event["price"],
                            shares=event["shares"],
                            unit=unit,
                            reason="unknown_unit" if unit is None else "ineligible_unit",
                        )
                    )
        source.write_csv(output / "lot_exceptions.csv", exceptions)
    for field in ("nav", "events", "trades", "exceptions"):
        source.write_csv(
            output / (field + ".csv"),
            [
                dict(initial_capital=cap, **r)
                for (cap, _), acct in accounts.items()
                for r in getattr(acct, field)
            ],
        )
    diff = []
    for cap in CAPITALS:
        z = {r["scope"]: r for r in summary if r["initial_capital"] == cap}
        no, yes = z["recent_nojsf"], z["recent_jsf"]
        diff.append(
            dict(
                initial_capital=cap,
                no_jsf_net=no["net"],
                jsf_net=yes["net"],
                difference=no["net"] - yes["net"],
                no_jsf_return_pct=no["total_return_pct"],
                jsf_return_pct=yes["total_return_pct"],
                no_jsf_dd_pct=no["max_dd_pct"],
                jsf_dd_pct=yes["max_dd_pct"],
            )
        )
    source.write_csv(output / "paired.csv", diff)
    receipt = dict(
        price_files=market.used_hashes,
        rank_files={n: source.file_hash(ranks_dir / n) for n in ("candidates.csv", "signals.csv")},
        jsf_ranks=source.file_hash(prior / "ranks.csv"),
        jsf_boundary=source.file_hash(prior.parent / "boundary/boundary_proof.json"),
        actions=source.file_hash(action_terms) if action_terms else None,
        board_lots=source.file_hash(board_lots) if board_lots else None,
        compared_to_prior=validate_reference(accounts, prior, source),
        equal_rank_days=sum(jsf[d] == selections[d] for d in jsf),
    )
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(
        [
            (s["initial_capital"], s["scope"], round(s["net"]), round(s["max_dd_pct"], 2))
            for s in summary
        ]
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for k in ("project", "ranks", "out"):
        p.add_argument("--" + k, required=True, type=Path)
    p.add_argument(
        "--actions",
        type=Path,
        default=Path(__file__).with_name("verified_entry_consolidations.json"),
    )
    p.add_argument("--board-lots", type=Path)
    args = p.parse_args()
    run(args.project.resolve(), args.ranks, args.out, args.actions, args.board_lots)
