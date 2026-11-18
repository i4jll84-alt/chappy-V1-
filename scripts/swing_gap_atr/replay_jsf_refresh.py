"""Replay the November 2026 daily swing candidates with newly retrieved JSF search snapshots.

J-Quants inputs keep their existing selection-date cutoff. Results have retrospective JSF vintage.
"""

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from unittest.mock import patch


def run(project, refreshed, accounts, output, boundary=None):
    sys.path.insert(0, str(project / "src"))
    sys.path.insert(0, str(project / "scripts/swing_gap_atr"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cap_backtest as engine
    import continuous as source

    from trading_system.research import swing_credit
    from trading_system.research import swing_forward as sf

    archive = project / "data/offline-verification/swing-agent-replay-20261113"
    study = project / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs"
    fixed = source.frozen_ranks(archive / "archive", study)
    old_nov, timely = source.november_ranks(archive)
    cutoffs, proofs, _ = source.november_extension(project, sf, old_nov, timely)

    captures = {}
    capture_proofs = {}
    for day in sorted({max(fixed), *old_nov}):
        folder = refreshed / "data/swing-forward/credit" / day
        meta = source.read(folder / "manifest.json")
        rows, _ = swing_credit.read_credit(
            refreshed, day, datetime.fromisoformat(meta["available_at"])
        )
        captures[day] = rows
        capture_proofs[day] = {
            key: meta[key] for key in ("hashes", "available_at", "source_published_at")
        }

    # Point-in-time market data is loaded by the owning screener. Only the JSF
    # reader uses the specified late capture, never the live collection process.
    def retrospective_credit(_project, day, _cutoff):
        return captures[day], capture_proofs[day]

    new_ranks = {}
    ranks_report = []
    for day, old in sorted(old_nov.items()):
        price_cutoff = datetime.fromisoformat(day + "T23:59:59+09:00")
        with patch.object(sf, "read_credit", retrospective_credit):
            screened = sf.screen(project, price_cutoff.date(), price_cutoff)
        assert screened["credit_complete"]
        pool = screened["candidates"]
        current = [r["code"] for r in pool][:5]
        assert current == old["current"], (day, current, old["current"])
        selected = [r["code"] for r in pool if not r["plus3_exclusions"]][:5]
        new_ranks[day] = {"current": current, "plus3": selected}
        ranks_report.append(
            dict(
                decision=day,
                original_timely=bool(timely[day]),
                original_count=len(old["plus3"]),
                refreshed_count=len(selected),
                original_codes=" ".join(old["plus3"]),
                refreshed_codes=" ".join(selected),
                identical=selected == old["plus3"],
            )
        )
    initial_days = {
        "history": min(fixed),
        "history_zero_recovery": min(fixed),
        "november": min(old_nov),
    }
    ranks = {**fixed, **new_ranks}
    if boundary is not None:
        proof = source.read(boundary)
        if proof["decision"] != "2023-10-31" or proof["complete"] is not True:
            raise ValueError("The prior-day rank prefix was not verified")
        codes = proof["selected_codes"]
        if len(codes) != 5 or len(set(codes)) != 5 or any(len(c) != 5 for c in codes):
            raise ValueError("Invalid prior-day selection")
        ranks[proof["decision"]] = {"plus3": codes}
        initial_days["extended_history"] = proof["decision"]
        initial_days["extended_zero_recovery"] = proof["decision"]
        capture_proofs["boundary_proof_sha256"] = source.file_hash(boundary)
    all_ranks = dict(sorted(ranks.items()))
    needed = {c for arms in all_ranks.values() for c in arms["plus3"]}
    market = source.Market(project, study, needed, cutoffs, proofs)
    first = market.indices[min(all_ranks)]
    last = market.indices[max(all_ranks)]
    assert market.sessions[first : last + 1] == list(all_ranks)
    caps = (1_000_000, 2_000_000, 3_000_000)
    ledgers = {
        (c, scope): engine.Account("morning", None, scope, cash=Decimal(c))
        for c in caps
        for scope in initial_days
    }
    for (_, scope), ledger in ledgers.items():
        ledger.snapshot(initial_days[scope], Decimal(0), 0, 0, 0)
    for i in range(first, last + 1):
        d, entry, due = market.sessions[i], market.sessions[i + 1], market.sessions[i + 7]
        picks = engine.reference_list(market, d, all_ranks[d]["plus3"])
        bars = market.bars(entry)
        for (_, scope), ledger in ledgers.items():
            if d >= initial_days[scope]:
                engine.run_day(ledger, entry, due, picks, bars, sf)

    output.mkdir(parents=True, exist_ok=True)
    source.write_csv(output / "ranks.csv", ranks_report)
    results, annual = [], []
    last_mark = market.sessions[last + 1]
    for capital in caps:
        old_summary = source.rows(accounts / str(capital) / "summary.csv")
        before = [
            r
            for r in source.rows(accounts / str(capital) / "nav.csv")
            if r["scope"] == "history" and r["date"] <= min(old_nov)
        ]
        repeated = [r for r in ledgers[capital, "history"].nav if r["date"] <= min(old_nov)]
        assert len(before) == len(repeated)
        for a, b in zip(before, repeated, strict=True):
            assert a["date"] == b["date"]
            for key in ("nav", "cash", "pos_count", "market_value"):
                assert Decimal(a[key]) == Decimal(b[key]), (capital, a["date"], key)
        for scope in initial_days:
            acct = ledgers[capital, scope]
            summary = engine.metrics(acct, last_mark, "full")
            base = next(
                (r for r in old_summary if (r["scope"], r["period"]) == (scope, "full")), None
            )
            results.append(
                dict(
                    initial_capital=capital,
                    **summary,
                    prior_net=base["net"] if base else None,
                    net_difference=summary["net"] - Decimal(base["net"]) if base else None,
                )
            )
        for scope in ("history", "extended_history"):
            if scope not in initial_days:
                continue
            start = Decimal(capital)
            for year, marks in groupby(
                ledgers[capital, scope].nav[1:], key=lambda r: r["date"][:4]
            ):
                end = list(marks)[-1]["nav"]
                annual.append(
                    dict(
                        initial_capital=capital,
                        scope=scope,
                        year=year,
                        net=end - start,
                        return_pct=(end / start - 1) * 100,
                    )
                )
                start = end
    source.write_csv(output / "capital_summary.csv", results)
    if boundary is not None:
        fields = (
            "initial_capital",
            "first_mark",
            "last_mark",
            "annual_return_intervals",
            "nav",
            "net",
            "total_return_pct",
            "max_dd_pct",
            "avg_utilization_pct",
            "buys",
            "sells",
            "cash_skips",
            "fallback_exits",
        )
        source.write_csv(
            output / "extended_comparison.csv",
            [{k: r[k] for k in fields} for r in results if r["scope"] == "extended_history"],
        )
        prefix = market.sessions[market.indices["2016-11-18"] : first]
        conditional = []
        for capital in caps:
            observed = ledgers[capital, "extended_history"].nav
            for day in prefix:
                conditional.append(
                    dict(
                        initial_capital=capital,
                        date=day,
                        nav=capital,
                        positions=0,
                        basis="assumed_cash_no_archive",
                    )
                )
            for row in observed:
                conditional.append(
                    dict(
                        initial_capital=capital,
                        date=row["date"],
                        nav=row["nav"],
                        positions=row["pos_count"],
                        basis="retrospective_account",
                    )
                )
            assert len(prefix) + len(observed) == 2441
        source.write_csv(output / "conditional_ten_year_nav.csv", conditional)
    source.write_csv(output / "capital_by_year.csv", annual)
    for collection in ("nav", "events", "exceptions", "trades"):
        source.write_csv(
            output / (collection + ".csv"),
            [
                {"initial_capital": c, **row}
                for (c, _), a in ledgers.items()
                for row in getattr(a, collection)
            ],
        )
    (output / "sources.json").write_text(
        json.dumps(capture_proofs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("project", "captures", "accounts", "out"):
        parser.add_argument("--" + key, required=True, type=Path)
    parser.add_argument(
        "--boundary", type=Path, help="Verified 2023-10-31 rank proof, if available"
    )
    args = parser.parse_args()
    run(args.project.resolve(), args.captures, args.accounts, args.out, args.boundary)
