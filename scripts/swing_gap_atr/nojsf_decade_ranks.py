"""Rank Japanese swing candidates on every decision day with only the JSF filter removed."""

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

FIRST = "2016-11-18"
LAST_MARK = "2026-11-18"
BULK_END = "2026-10-30"
BAR_FIELDS = ("Date", "Code", "H", "L", "C", "Va", "AdjFactor", "ExRT")
MASTER_FIELDS = ("Date", "Code", "Mkt", "ProdCat")
ENDPOINTS = {
    "/equities/bars/daily": "bars",
    "/equities/master": "master",
    "/markets/calendar": "calendar",
}

# Rolling calculations use original, same-day prices and complete JPX-session windows.
# Preserve strict comparisons by summing prices and turnover as decimals.
FEATURES = """
WITH px AS (
  SELECT b.Date,b.Code,s.idx,
    try_cast(b.C AS DECIMAL(18,3)) AS c, try_cast(b.H AS DECIMAL(18,3)) AS h,
    try_cast(b.L AS DECIMAL(18,3)) AS l, try_cast(b.Va AS DECIMAL(23,0)) AS va,
    try_cast(b.AdjFactor AS DOUBLE)=1 AND coalesce(b.ExRT,'')='' AS clean
  FROM bars b JOIN sessions s USING (Date)
  WHERE b.Date BETWEEN ? AND ?
), ranges AS (
  SELECT *, lag(c) OVER (PARTITION BY Code ORDER BY idx) AS prev FROM px
), features AS (
  SELECT *, lag(idx,20) OVER base AS old_idx, lag(c,20) OVER base AS c0,
    count(*) FILTER (WHERE c>0) OVER (base ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS priced,
    count(*) FILTER (WHERE clean) OVER (base ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS regular,
    sum(c) OVER (base ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS c5,
    sum(c) OVER (base ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS c20,
    sum(va) OVER (base ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS va5,
    count(*) FILTER (WHERE va>0) OVER
        (base ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS positive_va,
    sum(greatest(h-l, abs(h-prev), abs(l-prev))) OVER
        (base ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS sum_tr,
    count(*) FILTER (WHERE h>=l AND l>0 AND prev>0 AND c>0 AND clean) OVER
        (base ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS tr_count
  FROM ranges WINDOW base AS (PARTITION BY Code ORDER BY idx)
)
SELECT f.Date,f.Code,c,c0,CASE WHEN tr_count=14 AND sum_tr>0 THEN sum_tr END AS sum14
FROM features f JOIN master m USING(Date,Code)
WHERE f.Date BETWEEN ? AND ?
  AND m.ProdCat='011' AND right(f.Code,1)='0'
  AND ((f.Date<'2022-04-04' AND m.Mkt IN ('0101','0102','0104','0106','0107'))
    OR (f.Date>='2022-04-04' AND m.Mkt IN ('0111','0112','0113')))
  AND idx-old_idx=20 AND priced=21 AND regular=20
  AND va>=100000000 AND c*100*1.0005+10<=200000 AND c>c0
  AND c*5>c5 AND c5*4>c20 AND positive_va=5 AND va*5>va5
"""


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=records[0])
        writer.writeheader()
        writer.writerows(records)


def load_inputs(db, project):
    from trading_system.research.daytrade_universe import dated_inputs

    jq = project / "data/jquants"
    history = jq / "history"
    manifest = json.loads((history / "quality-20261031.json").read_bytes())
    assert manifest["end"] == BULK_END and not any(manifest["checks"].values())
    groups = defaultdict(list)
    hashes = {}
    for metadata in manifest["input_manifest"]:
        name = ENDPOINTS.get(metadata["endpoint"])
        if name:
            filepath = history / metadata["parquet_path"]
            digest = sha(filepath)
            assert digest == metadata["parquet_sha256"]
            groups[name].append(str(filepath))
            hashes[str(filepath.relative_to(project))] = digest
    for name, files in groups.items():
        db.from_parquet(files).create_view("bulk_" + name)
    db.execute(
        "CREATE TEMP TABLE sessions AS SELECT Date, row_number() OVER (ORDER BY Date) AS idx "
        "FROM bulk_calendar WHERE HolDiv IN ('1','2')"
    )
    sessions = [r[0] for r in db.execute("SELECT Date FROM sessions ORDER BY idx").fetchall()]
    assert len(sessions) == len(set(sessions))
    daily = {"bars": [], "master": []}
    for d in (t for t in sessions if BULK_END < t < LAST_MARK):
        cutoff = datetime.fromisoformat(d + "T23:59:59+09:00")
        quotes, master, refs = dated_inputs(jq, [d], cutoff)
        for source in refs:
            key = source.get("manifest") or source["page"]
            if key in hashes:
                assert hashes[key] == source["sha256"]
            hashes[key] = source["sha256"]
        for values in quotes.values():
            (record,) = values
            daily["bars"].append([record.get(f) for f in BAR_FIELDS])
        daily["master"].extend([[m.get(f) for f in MASTER_FIELDS] for m in master.values()])
    for name, fields in (("bars", BAR_FIELDS), ("master", MASTER_FIELDS)):
        array = pa.table(
            {
                field: pa.array([None if v is None else str(v) for v in col], type=pa.string())
                for field, col in zip(fields, zip(*daily[name]), strict=True)
            }
        )
        db.register("latest_frame", array)
        db.execute(f"CREATE TEMP TABLE nov_{name} AS SELECT * FROM latest_frame")
        db.unregister("latest_frame")
        fields_sql = ",".join(fields)
        db.execute(
            f"CREATE TEMP VIEW {name} AS SELECT {fields_sql} FROM bulk_{name} "
            f"WHERE Date<='{BULK_END}' UNION ALL SELECT {fields_sql} FROM nov_{name}"
        )
    return sessions, hashes


def verify_archive(project, signals, candidates):
    folder = project / "data/offline-verification/swing-gap-atr-variants-20261118/nojsf"
    reference = read_csv(folder / "daily_candidate_counts.csv")
    archive = defaultdict(list)
    for row in read_csv(folder / "ranks.csv"):
        archive[row["date"]].append((int(row["rank"]), row["code"]))
    frozen_days = len(archive)
    # The previous report saved ranked names for its 709 common episodes. Recover
    # the other 21 directly from its sealed date-specific cross sections.
    sealed = project / "data/offline-verification/swing-agent-replay-20261113/archive"
    key = json.loads((sealed / "decode.json").read_bytes())
    for token, meta in key["sets"].items():
        day = meta["date"]
        if day in archive:
            continue
        path = sealed / "inputs" / (token + ".parquet")
        assert sha(path) == meta["sha"]
        eligible = []
        for row in pq.read_table(path).to_pylist():
            c, c0 = Decimal(str(row["c"])), Decimal(str(row["c0"]))
            if not (
                c > c0 > 0
                and row["va"] >= 100_000_000
                and c * 100 * Decimal("1.0005") + 10 <= 200_000
            ):
                continue
            if (
                row["sum_va5"] is not None
                and row["va"] * 5 > row["sum_va5"] > 0
                and c * 20 > Decimal(str(row["sum5"])) * 4 > Decimal(str(row["sum20"]))
            ):
                eligible.append((-Fraction(c) / Fraction(c0), key["ids"][row["id"]]))
        archive[day] = [(rank, code) for rank, (_, code) in enumerate(sorted(eligible)[:5], 1)]
    by_day = {r["decision"]: r for r in signals}
    actual = defaultdict(list)
    for r in candidates:
        actual[r["decision"]].append((r["rank"], r["code"]))
    assert len(reference) == 730
    for row in reference:
        day = row["date"]
        assert by_day[day]["eligible_count"] == int(row["technical_eligible"]), day
        assert actual[day] == archive[day], day
    return {
        "decisions": len(reference),
        "saved_episode_rank_days": frozen_days,
        "additional_sealed_days": len(archive) - frozen_days,
        "candidate_rows": sum(map(len, archive.values())),
    }


def run(project, out):
    sys.path.insert(0, str(project / "src"))
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        sessions, hashes = load_inputs(db, project)
        first, last = sessions.index(FIRST), sessions.index(LAST_MARK)
        signals, candidates = [], []
        for i in range(first, last, 63):
            end = min(i + 63, last)
            batch = db.execute(
                FEATURES, [sessions[i - 20], sessions[end - 1], sessions[i], sessions[end - 1]]
            )
            pools = defaultdict(list)
            for day, code, close, old, total in batch.fetchall():
                pools[day].append((Fraction(close) / Fraction(old), code, close, total))
            for j in range(i, end):
                day = sessions[j]
                members = pools[day]
                assert len(members) == len({r[1] for r in members})
                members.sort(key=lambda r: (-r[0], r[1]))
                dates = dict(decision=day, entry=sessions[j + 1], due=sessions[j + 7])
                signals.append(
                    dict(**dates, candidate_count=min(5, len(members)), eligible_count=len(members))
                )
                for n, (_, code, close, total) in enumerate(members[:5], 1):
                    candidates.append(
                        dict(
                            **dates,
                            rank=n,
                            code=code,
                            prev_close=close,
                            atr14=total / Decimal(14) if total is not None else None,
                        )
                    )
    out.mkdir(parents=True, exist_ok=True)
    save_csv(out / "signals.csv", signals)
    save_csv(out / "candidates.csv", candidates)
    checked = verify_archive(project, signals, candidates)
    metadata = dict(
        start=signals[0]["decision"],
        end=signals[-1]["decision"],
        last_mark=LAST_MARK,
        decisions=len(signals),
        selections=len(candidates),
        no_candidate_days=[r["decision"] for r in signals if not r["candidate_count"]],
        missing_atr=sum(r["atr14"] is None for r in candidates),
        legacy_no_jsf_reconciled=checked,
        sources=hashes,
    )
    (out / "rank_inputs.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps({k: v for k, v in metadata.items() if k != "sources"}, ensure_ascii=False),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.project.resolve(), args.out)
