"""Check top-ranked names, ATR and corporate actions around selected entry windows."""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import nojsf_decade_ranks as rankings


def audit(project, out):
    sys.path.insert(0, str(project / "src"))
    from trading_system.research import swing_forward as sf
    from trading_system.research.daytrade_universe import dated_inputs

    candidates = rankings.read_csv(out / "candidates.csv")
    lookup = {(r["decision"], r["code"]): r for r in candidates}
    names = defaultdict(list)
    for r in candidates:
        names[r["decision"]].append(r["code"])
    counts = {
        r["decision"]: int(r["eligible_count"]) for r in rankings.read_csv(out / "signals.csv")
    }
    offline = project / "data/offline-verification/swing-gap-atr-variants-20261118/nojsf"
    tests = 0
    for row in rankings.read_csv(offline / "orders.csv"):
        if row["atr14"]:
            actual = Decimal(lookup[row["date"], row["code"]]["atr14"])
            original = Decimal(row["atr14"])
            if row["entry_day"] == "2025-07-30" and row["code"] == "55270":
                # Prior recorded orders express reference ATR on the split's entry-day scale.
                original *= 3
            assert abs(actual - original) <= Decimal("1e-20"), (
                row["date"],
                row["code"],
                actual,
                original,
            )
            tests += 1
    direct_days = []

    def compare(day, pool):
        usable = [
            r
            for r in pool
            if "ma" not in r["plus3_exclusions"] and "turnover_growth" not in r["plus3_exclusions"]
        ]
        assert len(usable) == counts[day] and [r["code"] for r in usable[:5]] == names[day], day
        direct_days.append(day)

    for d in counts:
        if d > "2026-10-30":
            cutoff = datetime.fromisoformat(d + "T23:59:59+09:00")
            compare(d, sf.screen(project, cutoff.date(), cutoff)["candidates"])
    flags = []
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        sessions, _ = rankings.load_inputs(db, project)
        for d in (
            "2016-11-18",
            "2017-06-30",
            "2018-09-28",
            "2018-10-01",
            "2020-10-01",
            "2020-10-30",
            "2022-04-01",
            "2022-04-04",
            "2023-10-31",
        ):
            i = sessions.index(d)
            window = sessions[i - 20 : i + 1]
            fields = "Date Code C Va ExRT AdjFactor".split()
            q = db.execute(
                "SELECT " + ",".join(fields) + " FROM bars WHERE Date BETWEEN ? AND ?",
                [window[0], d],
            )
            history = defaultdict(dict)
            for rec in q.fetchall():
                history[rec[0]][rec[1]] = dict(zip(fields, rec))
            m = {}
            for code, mkt, pc in db.execute(
                "SELECT Code,Mkt,ProdCat FROM master WHERE Date=?", [d]
            ).fetchall():
                market = (
                    "0111"
                    if d < "2022-04-04" and mkt in ("0101", "0102", "0104", "0106", "0107")
                    else mkt
                )
                m[code] = dict(Mkt=market, ProdCat=pc, CoName="")
            pool, _ = sf.ranks(history, m, window, None)
            compare(d, pool)
        db.execute(
            "CREATE TEMP TABLE chosen AS SELECT * FROM read_csv(?,all_varchar=true)",
            [str(out / "candidates.csv")],
        )
        result = db.execute("""
            SELECT decision, rank, c.code, c.entry, c.due, b.Date AS event_date,
              b.AdjFactor AS adj_factor,b.ExRT AS ex_rt,
              CASE WHEN b.Date=c.entry THEN 'entry' ELSE 'possible_hold' END AS exposure
            FROM chosen c JOIN bars b ON b.Code=c.code AND b.Date BETWEEN c.entry AND c.due
            WHERE try_cast(b.AdjFactor AS DOUBLE) IS DISTINCT FROM 1 OR coalesce(b.ExRT,'')!=''
            ORDER BY b.Date,c.code,decision
        """)
        columns = [r[0] for r in result.description]
        flags = [dict(zip(columns, r)) for r in result.fetchall()]
    end = rankings.LAST_MARK
    quotes, final_master, _ = dated_inputs(
        project / "data/jquants", [end], datetime.fromisoformat(end + "T23:59:59+09:00")
    )
    for row in candidates:
        if row["entry"] <= end <= row["due"] and row["code"] in quotes:
            (bar,) = quotes[row["code"]]
            if Decimal(str(bar["AdjFactor"])) != 1 or bar["ExRT"] not in ("", None):
                flags.append(
                    dict(
                        decision=row["decision"],
                        rank=row["rank"],
                        code=row["code"],
                        entry=row["entry"],
                        due=row["due"],
                        event_date=end,
                        adj_factor=bar["AdjFactor"],
                        ex_rt=bar["ExRT"],
                        exposure="entry" if end == row["entry"] else "possible_hold",
                    )
                )
    all_names = {r["code"] for r in candidates}
    rankings.save_csv(out / "candidate_rights.csv", flags)
    entry_rights = [r for r in flags if r["exposure"] == "entry"]
    result = dict(
        atr_matches=tests,
        independent_dates=direct_days,
        all_possible_right_rows=len(flags),
        distinct_candidate_codes=len(all_names),
        distinct_historical_codes_absent_last_master=len(all_names - set(final_master)),
        selections_absent_last_master=sum(r["code"] not in final_master for r in candidates),
        entry_corporate_actions=entry_rights,
    )
    (out / "rank_validation.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    audit(args.project.resolve(), args.out.resolve())
