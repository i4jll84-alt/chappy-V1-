"""Re-rank the entire daily V1 pool without JSF, then replay the frozen 709 episodes."""

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from decimal import Decimal as D
from fractions import Fraction
from pathlib import Path
from statistics import mean, median

import duckdb
import pyarrow.parquet as pq


def read_json(path):
    return json.loads(path.read_bytes())


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def save_json(path, obj):
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False, default=str, sort_keys=True), encoding="utf8"
    )


def save_csv(path, records):
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_source_paths(root, outputs, first, last):
    hist = root / "data/jquants/history"
    frozen = read_json(outputs / "sources.json")
    assert (
        not (hist / "history.sqlite-wal").exists()
        or not (hist / "history.sqlite-wal").stat().st_size
    )
    groups = {}
    checked = {}
    with closing(
        sqlite3.connect((hist / "history.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as db:
        for key, endpoint, lower in (
            ("calendar", "/markets/calendar", first),
            ("bars", "/equities/bars/daily", "2023-10-02"),
            ("master", "/equities/master", first),
        ):
            rows = db.execute(
                "SELECT raw_path,sha256,parquet_path,parquet_sha256 FROM bulk_files "
                "WHERE state='complete' AND endpoint=? AND min_date<=? AND max_date>=? "
                "ORDER BY min_date,object_key",
                (endpoint, last, lower),
            )
            parts = []
            for raw, raw_sha, packed, packed_sha in rows:
                for rel, expected in ((raw, raw_sha), (packed, packed_sha)):
                    path = (hist / rel).resolve()
                    assert path.is_relative_to(hist)
                    local = str(path.relative_to(root))
                    assert sha(path) == expected == frozen[local], path
                    checked[local] = expected
                parts.append(str(hist / packed))
            assert parts, endpoint
            groups[key] = parts
    return groups, checked


# Only as-of-date master/classification and the trailing price/turnover window are joined.
# A missing session, missing price, or adjustment in the return interval disqualifies a row.
RAW_POOL_QUERY = """
WITH prices AS (
 SELECT b.Date,b.Code,j.idx,try_cast(b.C AS DECIMAL(18,3)) AS c,
   try_cast(b.Va AS DECIMAL(22,0)) AS va,
   (try_cast(b.AdjFactor AS DOUBLE)=1 AND coalesce(b.ExRT,'')='') AS clean
 FROM bars b JOIN sessions j ON j.Date=b.Date WHERE b.Date BETWEEN ? AND ?
), windows AS (
 SELECT *,lag(idx,20) OVER base AS old_idx,lag(c,20) OVER base AS c0,
   min(c) OVER (base ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS min_close,
   count(c) OVER (base ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS num_close,
   count(*) FILTER (WHERE clean) OVER
     (base ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS clean_count,
   sum(c) OVER (base ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS sum5,
   sum(c) OVER (base ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS sum20,
   sum(va) OVER (base ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS vsum,
   count(*) FILTER (WHERE va>0) OVER (base ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS vcount
 FROM prices WINDOW base AS (PARTITION BY Code ORDER BY idx)
)
SELECT w.Date,w.Code,w.c0,w.c,
 (w.c*20>w.sum5*4 AND w.sum5*4>w.sum20 AND w.vcount=5 AND w.va*5>w.vsum) AS tech
FROM windows w JOIN master m ON m.Date=w.Date AND m.Code=w.Code
WHERE w.Date BETWEEN ? AND ? AND m.Mkt IN ('0111','0112','0113') AND m.ProdCat='011'
 AND right(w.Code,1)='0' AND w.idx-w.old_idx=20 AND w.num_close=21 AND w.min_close>0
 AND w.clean_count=20 AND w.va>=100000000 AND w.c>w.c0
 AND w.c*100*1.0005+10<=200000
ORDER BY w.Date,w.Code
"""


def raw_pools(parts, decision_dates):
    """Rebuild every day's complete base and technical pool from J-Quants bars and master."""
    result = defaultdict(dict)
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        for key, files in parts.items():
            db.from_parquet(files).create_view(key)
        db.execute(
            "CREATE TEMP TABLE sessions AS SELECT Date,row_number() OVER(ORDER BY Date) "
            "AS idx FROM calendar WHERE HolDiv IN ('1','2')"
        )
        sessions = [r[0] for r in db.execute("SELECT Date FROM sessions ORDER BY idx").fetchall()]
        indices = {d: i for i, d in enumerate(sessions)}
        assert len(indices) == len(sessions)
        assert (
            sessions[indices[decision_dates[0]] : indices[decision_dates[-1]] + 1] == decision_dates
        )
        # Separate batches cap memory, each with its own full 20-session warm-up.
        for start in range(0, len(decision_dates), 42):
            chunk = decision_dates[start : start + 42]
            warm = sessions[indices[chunk[0]] - 20]
            for day, code, c0, close, technical in db.execute(
                RAW_POOL_QUERY, [warm, chunk[-1], chunk[0], chunk[-1]]
            ).fetchall():
                assert code not in result[day], (day, code)
                result[day][code] = (Fraction(close) / Fraction(c0), bool(technical))
    return result


def rank_pools(project, archive, decision_dates, pools, expected):
    decode = read_json(archive / "decode.json")
    assert sorted(m["date"] for m in decode["sets"].values()) == decision_dates
    rank_rows = []
    metadata = []
    verified = {}
    for token, m in sorted(decode["sets"].items(), key=lambda pair: pair[1]["date"]):
        day = m["date"]
        path = archive / "inputs" / (token + ".parquet")
        assert sha(path) == m["sha"]
        verified[str(path.relative_to(project))] = m["sha"]
        source = pq.read_table(path).to_pylist()
        assert len(source) == m["rows"]
        archived = {}
        credit_pass = set()
        for row in source:
            code = decode["ids"][row["id"]]
            close, start = D(str(row["c"])), D(str(row["c0"]))
            if not (
                close > start > 0
                and row["va"] >= 100_000_000
                and close * 100 * D("1.0005") + 10 <= 200_000
            ):
                continue
            assert code not in archived
            tech = (
                row["sum_va5"] is not None
                and row["va"] * 5 > row["sum_va5"] > 0
                and close * 20 > D(str(row["sum5"])) * 4 > D(str(row["sum20"]))
            )
            archived[code] = (Fraction(close) / Fraction(start), bool(tech))
            credit = [row[k] for k in ("f0", "l0", "f1", "l1")]
            if tech and all(v is not None and v >= 0 for v in credit) and max(credit) > 0:
                f0, l0, f1, l1 = credit
                if f1 - l1 <= f0 - l0 and row["restricted"] is False:
                    credit_pass.add(code)
        # Compare all eligible codes, return ratios and technical flags with the primary data.
        assert archived == pools[day], day
        ranked = sorted(pools[day], key=lambda code: (-pools[day][code][0], code))
        tech = [code for code in ranked if pools[day][code][1]]
        plus = [code for code in tech if code in credit_pass]
        assert ranked[:5] == expected[day, "current"] and plus[:5] == expected[day, "plus3"], day
        metadata.append(
            dict(
                date=day,
                archive_broad_rows=len(source),
                base_eligible=len(ranked),
                technical_eligible=len(tech),
                with_jsf_eligible=len(plus),
                unchanged_ranks=plus[:5] == tech[:5],
                overlap=len(set(plus[:5]) & set(tech[:5])),
            )
        )
        for j, code in enumerate(tech[:5], 1):
            rank_rows.append(
                dict(
                    date=day,
                    rank=j,
                    code=code,
                    original_jsf_pass=(code in credit_pass),
                    original_plus3_rank=plus.index(code) + 1 if code in plus[:5] else "",
                )
            )
    return rank_rows, metadata, verified


def summary(windows, baseline, dates):
    complete = [d for d in dates if windows[d]["status"] == "ok"]
    result = {
        "planned": len(dates),
        "resolved": len(complete),
        "unresolved": len(dates) - len(complete),
    }
    if not complete:
        return result
    vals = [windows[d]["net"] for d in complete]
    comps = [D(baseline[d]["net"]) for d in complete]
    diffs = [a - b for a, b in zip(vals, comps, strict=True)]
    result.update(
        mean_net=mean(vals),
        median_net=median(vals),
        worst_net=min(vals),
        positive=sum(x > 0 for x in vals),
        mean_episode_max_dd_pct=mean(windows[d]["dd_pct"] for d in complete),
        baseline_mean_net=mean(comps),
        mean_delta=mean(diffs),
        median_delta=median(diffs),
        better=sum(x > 0 for x in diffs),
        tied=diffs.count(0),
        worse=sum(x < 0 for x in diffs),
        baseline_mean_episode_max_dd_pct=mean(D(baseline[d]["dd_pct"]) for d in complete),
    )
    for k in (
        "candidates",
        "buys",
        "skips",
        "gap_skips",
        "atr_skips",
        "open_skips",
        "cash_skips",
        "stop_touch",
        "stop_open",
        "scheduled",
        "fees",
        "slippage",
    ):
        result[k] = sum(windows[d][k] for d in complete)
    return result


def odd_lot_evidence(engine, market, records, actions):
    """Document the consolidation; a board-lot bar does not price a ten-share exit."""
    code, ex_date = "88940", "2024-10-17"
    day_bar = market[ex_date, code]
    assert day_bar[5] == D(10) and day_bar[6] == "2"
    affected = [
        dict(
            start=r["date"],
            buy_date=r["entry_day"],
            price=r["buy"],
            entry_cash_debit=r["buy"] * engine.SHARES + r["entry_fee"] + r["entry_slippage"],
        )
        for r in actions
        if r["code"] == code and r["issue"] and r["issue"].startswith(ex_date)
    ]
    assert [r["start"] for r in affected] == ["2024-10-10", "2024-10-11"]
    assert all(records[r["start"]]["status"] == "unresolved" for r in affected)
    return dict(
        code=code,
        issuer="REVOLUTION",
        ex_date=ex_date,
        effective_date="2024-10-21",
        announced_vote_date="2024-09-30",
        shares_before=100,
        shares_after=10,
        official_disclosure=(
            "https://s3-ap-northeast-1.amazonaws.com/revolution-prod/files/ir_news/"
            "1713/57122a39ac15ab2cd2ddf84d5b5465b2.pdf?v=1727681959"
        ),
        last_cum_rights_day_source_url=(
            "https://search.sbisec.co.jp/v2/popwin/attention/stock/information/"
            "pop4040_240925_01.html"
        ),
        affected=affected,
        convention=(
            "The ordinary 100-share lot becomes 10 shares. Quote bars and the board-lot "
            "execution convention do not establish an odd-lot exit fill or its fees."
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="New directory")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Evidence checks require Python without -O.")
    project = args.project.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(project / "scripts/swing_gap_atr"))
    import replay_episodes as engine

    studies = project / "data/offline-verification"
    source = studies / "swing-periods-ma-jsf-20261113/outputs"
    original = studies / "swing-gap-atr-20261118/episodes"
    archive = studies / "swing-agent-replay-20261113/archive"
    schedules = {
        r["decision"]: r for r in read_csv(source / "coverage.csv") if r["reference"] == "False"
    }
    dates = sorted(schedules)
    paired = read_csv(original / "paired.csv")
    keep = [
        r["date"]
        for r in paired
        if all(r[s + "_status"] == "ok" for s in ("current", "plus3", "guarded"))
    ]
    assert (
        sorted(schedules) == [r["date"] for r in paired] and len(dates) == 730 and len(keep) == 709
    )
    allowed = set(keep)
    baseline = {
        r["date"]: r
        for r in read_csv(original / "windows.csv")
        if r["rule"] == "guarded" and r["date"] in allowed
    }
    expected = {
        (r["decision"], r["arm"]): r["machine"].split() for r in read_csv(archive / "orders.csv")
    }
    files, checked = load_source_paths(project, source, dates[0], dates[-1])
    pools = raw_pools(files, dates)
    rank_rows, metadata, archive_sources = rank_pools(project, archive, dates, pools, expected)
    checked.update(archive_sources)
    ranks = [r for r in rank_rows if r["date"] in allowed]
    save_csv(out / "ranks.csv", ranks)
    save_csv(out / "daily_candidate_counts.csv", metadata)
    picks = defaultdict(list)
    for r in ranks:
        picks[r["date"], "without_jsf"].append(r["code"])
    for d in keep:
        picks[d, "with_jsf"] = expected[d, "plus3"]
    days, idx, market, used = engine.load_bars(project, source, picks, schedules)
    checked.update(used)
    records, actions, matched = {}, [], 0
    frozen_orders = {
        (r["date"], int(r["rank"])): r
        for r in read_csv(original / "orders.csv")
        if r["rule"] == "guarded" and r["date"] in allowed
    }
    for d in keep:
        old, moves = engine.simulate(d, "guarded", picks[d, "with_jsf"], days, idx[d], market)
        assert len(moves) == len(picks[d, "with_jsf"])
        if moves:
            assert set(frozen_orders[d, 1]) == set(moves[0])
        for row in moves:
            stored = frozen_orders[d, row["rank"]]
            assert all(stored[k] == ("" if v is None else str(v)) for k, v in row.items()), (d, row)
            matched += 1
        assert all(baseline[d][k] == ("" if v is None else str(v)) for k, v in old.items()), d
        rec, trades = engine.simulate(d, "guarded", picks[d, "without_jsf"], days, idx[d], market)
        rec["rule"] = "without_jsf"
        for a in trades:
            a["rule"] = rec["rule"]
        records[d] = rec
        actions.extend(trades)
    assert matched == len(frozen_orders)
    save_csv(out / "windows.csv", list(records.values()))
    save_csv(out / "orders.csv", actions)
    for p in (
        source / "coverage.csv",
        source / "sources.json",
        original / "paired.csv",
        original / "windows.csv",
        original / "orders.csv",
        archive / "decode.json",
        archive / "orders.csv",
        Path(engine.__file__),
    ):
        checked[str(p.relative_to(project))] = sha(p)
    checked["script:replay_without_jsf.py"] = sha(Path(__file__))
    counts = Counter()
    for m in metadata:
        if m["date"] in allowed:
            for col in (
                "archive_broad_rows",
                "base_eligible",
                "technical_eligible",
                "with_jsf_eligible",
                "overlap",
                "unchanged_ranks",
            ):
                counts[col] += m[col]
    report = dict(
        frozen_starts=709,
        original_starts=730,
        planned_range=[keep[0], keep[-1]],
        full_base_pools_matched=len(pools),
        prior_rank_sets_matched=len(expected),
        prior_eligible_and_technical_flags_checked=sum(len(v) for v in pools.values()),
        old_portfolio_windows_matched=len(keep),
        old_ranked_action_rows_matched=matched,
        counts_709=dict(counts),
        verified_source_files=len(checked),
        standard_lot_exception=odd_lot_evidence(engine, market, records, actions),
        all=summary(records, baseline, keep),
        years={
            year: summary(records, baseline, [d for d in keep if d.startswith(year)])
            for year in sorted({d[:4] for d in keep})
        },
        offsets={
            str(i): summary(records, baseline, [d for d in dates[i::7] if d in allowed])
            for i in range(7)
        },
        unidentified_windows={
            d: records[d]["issues"] for d in keep if records[d]["status"] != "ok"
        },
        provenance=(
            "J-Quants dated domestic-equity master and checked bars for all eligible names; "
            "checked full archived decision features and original rankings. JSF snapshots "
            "originally acquired in bulk after historical decisions; prices represent archived "
            "versions. Independent starting capital per date."
        ),
    )
    save_json(out / "summary.json", report)
    save_json(out / "verified_sources.json", checked)
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("years", "offsets")},
            default=str,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
