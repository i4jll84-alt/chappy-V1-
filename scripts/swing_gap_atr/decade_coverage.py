"""Audit the fixed ten-year Japanese swing window and its daily input coverage."""

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import duckdb

START = "2016-11-18"
END = "2026-11-18"
BULK_END = "2026-10-30"
CURRENT = {"0111", "0112", "0113"}
FORMER = {"0101", "0102", "0104", "0106", "0107"}


def hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def write_csv(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=records[0])
        writer.writeheader()
        writer.writerows(records)


def market_stats(project):
    jq = project / "data/jquants"
    hist = jq / "history"
    quality = json.loads((hist / "quality-20261031.json").read_bytes())
    assert not any(quality["checks"].values())
    assert quality["start"] <= START and quality["end"] == BULK_END
    endpoints = {"/equities/bars/daily", "/equities/master", "/markets/calendar"}
    files = defaultdict(list)
    for x in quality["input_manifest"]:
        if x["endpoint"] in endpoints:
            path = hist / x["parquet_path"]
            assert hash_file(path) == x["parquet_sha256"]
            files[x["endpoint"]].append(str(path))
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        for ep, alias in (
            ("/equities/bars/daily", "prices"),
            ("/equities/master", "master"),
            ("/markets/calendar", "calendar"),
        ):
            db.from_parquet(files[ep]).create_view(alias)
        sessions = [
            r[0]
            for r in db.execute(
                "SELECT Date FROM calendar WHERE HolDiv IN ('1','2') ORDER BY Date"
            ).fetchall()
        ]
        stats = {
            d: [prices, positive, masters, modern, former]
            for d, prices, positive, masters, modern, former in db.execute("""
                WITH px AS (
                    SELECT Date, count(*) AS prices,
                           count(*) FILTER (WHERE try_cast(C AS DECIMAL(16,3))>0) AS positive
                    FROM prices GROUP BY Date
                ), m AS (
                    SELECT Date, count(*) AS masters,
                           count_if(ProdCat='011' AND ends_with(Code,'0')
                                    AND Mkt IN ('0111','0112','0113')) AS modern,
                           count_if(ProdCat='011' AND ends_with(Code,'0')
                                    AND Mkt IN ('0101','0102','0104','0106','0107')) AS former
                    FROM master GROUP BY Date
                )
                SELECT Date, prices, positive, masters, modern, former FROM px JOIN m USING(Date)
                ORDER BY Date
            """).fetchall()
        }
    assert len(sessions) == len(set(sessions))
    assert set(stats) == {d for d in sessions if quality["start"] <= d <= BULK_END}
    sys.path.insert(0, str(project / "src"))
    from trading_system.research.daytrade_universe import dated_inputs

    for d in (d for d in sessions if BULK_END < d <= END):
        columns, master, _ = dated_inputs(jq, [d], datetime.fromisoformat(d + "T23:59:59+09:00"))
        assert len(columns) == len(master)
        assert all(len(pair) == 1 for pair in columns.values())
        usable_master = [
            r for code, r in master.items() if code.endswith("0") and r["ProdCat"] == "011"
        ]
        stats[d] = [
            len(columns),
            sum(float(x[0].get("C") or 0) > 0 for x in columns.values()),
            len(master),
            sum(x["Mkt"] in CURRENT for x in usable_master),
            sum(x["Mkt"] in FORMER for x in usable_master),
        ]
    return sessions, stats, quality, sum(map(len, files.values()))


def run(project, output):
    project = project.resolve()
    sessions, stats, quality, sources = market_stats(project)
    first, last = sessions.index(START), sessions.index(END)
    eval_days = sessions[first : last + 1]
    warmup = sessions[first - 20 : first]
    assert len(warmup) == 20 and all(d in stats for d in warmup + eval_days)
    assert all(stats[d][0] == stats[d][2] and stats[d][0] > 0 for d in warmup + eval_days)
    ranks_path = project / "data/offline-verification/swing-position-caps-20261118/signals.csv"
    frozen_rows = rows(ranks_path)
    frozen = {r["decision"]: r for r in frozen_rows}
    assert len(frozen_rows) == len(frozen)
    jsf = project / "data/free-market/jsf-history"
    jsf_dates = {}
    for path in sorted((jsf / "days").glob("*.json")):
        meta = json.loads(path.read_bytes())
        assert meta["date"] == path.stem and meta["state"] == "checked"
        assert hash_file(jsf / meta["csv"]["path"]) == meta["csv"]["sha256"]
        jsf_dates[path.stem] = meta["available_at"]
    assert sorted(jsf_dates) == json.loads((jsf / "plan.json").read_bytes())["dates"]
    records = []
    for i in range(first, last):
        day, entry, previous = sessions[i], sessions[i + 1], sessions[i - 1]
        prior = frozen.get(day)
        genuine = prior is not None and prior["jsf_same_day_pair"] == "True"
        pair = previous in jsf_dates and day in jsf_dates
        if pair:
            assert prior is not None and prior["source"] == "archive" and not genuine
            state = "archived_pair_retrieved_later"
        elif prior and genuine:
            assert prior["source"] == "november"
            state = "timely_pair"
        elif prior:
            assert prior["source"] == "november" and int(prior["candidates"]) == 0
            state = "known_no_new_buy"
        else:
            state = "missing_history"
        if prior:
            assert prior["entry"] == entry
        bar, pos, master, current, former = stats[day]
        records.append(
            dict(
                decision=day,
                next_session=entry,
                preceding_session=previous,
                decision_year=int(day[:4]),
                price_rows=bar,
                nonempty_closes=pos,
                same_day_master_rows=master,
                current_market_ordinary=current,
                predecessor_market_ordinary=former,
                twenty_prior_sessions=int(all(d in stats for d in sessions[i - 20 : i])),
                next_price_rows=stats[entry][0],
                jsf_archive_previous=int(previous in jsf_dates),
                jsf_archive_current=int(day in jsf_dates),
                input_status=state,
                frozen_candidates=prior["candidates"] if prior else None,
            )
        )
    yearly = []
    for y in sorted({r["decision_year"] for r in records}):
        sub = [r for r in records if r["decision_year"] == y]
        counts = Counter(r["input_status"] for r in sub)
        yearly.append(
            dict(
                year=y,
                first_decision=sub[0]["decision"],
                last_decision=sub[-1]["decision"],
                decision_sessions=len(sub),
                missing_history=counts["missing_history"],
                archived_pair_retrieved_later=counts["archived_pair_retrieved_later"],
                timely_pair=counts["timely_pair"],
                known_no_new_buy=counts["known_no_new_buy"],
                evaluable_decisions=len(sub) - counts["missing_history"],
            )
        )
    summary = dict(
        first_mark=START,
        last_mark=END,
        sessions_including_start=len(eval_days),
        decisions=len(records),
        warmup_first=warmup[0],
        warmup_last=warmup[-1],
        history_price_and_same_day_master_start=quality["start"],
        bulk_end=BULK_END,
        nonempty_markets=all(
            r["current_market_ordinary"] + r["predecessor_market_ordinary"] > 0 for r in records
        ),
        status_counts=dict(Counter(r["input_status"] for r in records)),
        first_evaluable=min(frozen),
        last_evaluable=max(frozen),
        local_jsf_archive_count=len(jsf_dates),
        local_jsf_archive_first=min(jsf_dates),
        local_jsf_archive_last=max(jsf_dates),
        missing_first=next(
            r["decision"] for r in records if r["input_status"] == "missing_history"
        ),
        missing_last=next(
            r["decision"] for r in reversed(records) if r["input_status"] == "missing_history"
        ),
        first_modern_day=next(r["decision"] for r in records if r["current_market_ordinary"]),
        last_predecessor_day=next(
            r["decision"] for r in reversed(records) if r["predecessor_market_ordinary"]
        ),
        hashed_bulk_inputs=sources,
        audited_price_master_key_mismatches=[
            quality["checks"][field]
            for field in ("master_without_same_day_price", "price_without_same_day_master")
        ],
        evaluated_dates_sha256=hash_file(ranks_path),
        eleven_november_decisions={
            r["decision"]: r["input_status"] for r in records if r["decision"] > BULK_END
        },
        sessions_with_no_traded_close=[d for d in eval_days if not stats[d][1]],
        timezone="Asia/Tokyo",
    )
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "decade_coverage_days.csv", records)
    write_csv(output / "decade_coverage_by_year.csv", yearly)
    (output / "decade_coverage.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.project, args.out)
