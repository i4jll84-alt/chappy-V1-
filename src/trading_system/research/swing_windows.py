"""Build unadjusted open-to-open swing research inputs from a closed historical archive."""

import argparse
import hashlib
import json
import math
import os
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq

from trading_system.research.features import FEATURES

HORIZONS = (5, 10, 20)


def history_rows(db, root, endpoint, columns, lo, hi, manifest):
    rows = db.execute(
        "SELECT raw_path,sha256,parquet_path,parquet_sha256,available_at FROM bulk_files "
        "WHERE state='complete' AND endpoint=? AND min_date<=? AND max_date>=? "
        "ORDER BY min_date,object_key",
        (endpoint, hi, lo),
    )
    for raw, raw_hash, path, parquet_hash, available_at in rows:
        for rel, expected in ((raw, raw_hash), (path, parquet_hash)):
            target = (root / rel).resolve()
            if not target.is_relative_to(root):
                raise ValueError("unsafe_archive_path")
            with target.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != expected:
                    raise ValueError("source_hash_mismatch")
        manifest.append(
            dict(
                endpoint=endpoint,
                path=path,
                sha256=parquet_hash,
                raw_sha256=raw_hash,
                available_at=available_at,
            )
        )
        table = pq.read_table(root / path, columns=columns)
        for row in table.to_pylist():
            if lo <= row["Date"] <= hi:
                yield row


def price(v):
    if v in (None, ""):
        return None
    f = float(v)
    return f if math.isfinite(f) and f > 0 else None


def make_features(rows):
    if any(v is None or not price(v["C"]) for v in rows):
        return None, "incomplete_close_window"
    if any(price(v["AdjFactor"]) != 1 or v["ExRT"] not in (None, "") for v in rows[1:]):
        return None, "rights_in_feature_window"
    closes = [float(v["C"]) for v in rows]
    r = [b / a - 1 for a, b in zip(closes, closes[1:])]
    f = [closes[-1] / closes[-1 - k] - 1 for k in (1, 5, 20)]
    f.extend(closes[-1] / statistics.mean(closes[-k:]) - 1 for k in (5, 20))
    return dict(zip(FEATURES, [*f, statistics.stdev(r)], strict=True)), "ready"


def outcome(path, benchmark):
    if any(x is None for x in path):
        return None, "missing_or_delisted_row"
    if any(price(x["AdjFactor"]) != 1 or x["ExRT"] not in (None, "") for x in path[1:]):
        return None, "rights_during_holding"
    a, b = price(path[0]["O"]), price(path[-1]["O"])
    if not a or not b:
        return None, "missing_open"
    if not all(benchmark):
        return None, "missing_benchmark"
    return (b / a - 1, benchmark[1] / benchmark[0] - 1), "observed"


def build(root, out, first, last, through):
    root = root.resolve()
    if not first <= last <= through or out.exists():
        raise ValueError("invalid_dates_or_existing_result")
    dbpath = root / "history.sqlite"
    wal = root / "history.sqlite-wal"
    if wal.exists() and wal.stat().st_size:
        raise ValueError("archive_has_active_writes")
    sources = []
    prices = {}
    universe = defaultdict(dict)
    index = {}
    with sqlite3.connect(dbpath.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        calendar = {}
        for row in history_rows(
            db, root, "/markets/calendar", ["Date", "HolDiv"], "2000-01-01", "2099-12-31", sources
        ):
            if calendar.setdefault(row["Date"], row["HolDiv"]) != row["HolDiv"]:
                raise ValueError("calendar_conflict")
        days = sorted(x for x in calendar if calendar[x] in ("1", "2"))
        study = [x for x in days if first <= x <= last]
        if not study or days.index(study[0]) < 20 or days[-1] <= through:
            raise ValueError("incomplete_calendar")
        decisions = [
            list(g)[-1]
            for _, g in groupby(study, lambda x: date.fromisoformat(x).isocalendar()[:2])
        ]
        lo = days[days.index(study[0]) - 20]
        for row in history_rows(
            db,
            root,
            "/equities/bars/daily",
            ["Date", "Code", "O", "C", "ExRT", "AdjFactor", "Va"],
            lo,
            through,
            sources,
        ):
            key = row["Date"], row["Code"]
            if prices.setdefault(key, row) != row:
                raise ValueError("conflicting_daily_row")
        for row in history_rows(
            db,
            root,
            "/equities/master",
            ["Date", "Code", "Mkt", "ProdCat", "S33"],
            first,
            last,
            sources,
        ):
            if row["Date"] not in decisions:
                continue
            if universe[row["Date"]].setdefault(row["Code"], row) != row:
                raise ValueError("conflicting_master")
        for row in history_rows(
            db, root, "/indices/bars/daily/topix", ["Date", "O"], lo, through, sources
        ):
            if index.setdefault(row["Date"], price(row["O"])) != price(row["O"]):
                raise ValueError("benchmark_conflict")
    frows = []
    targets = []
    coverage = {}
    for decision in decisions:
        if not universe[decision]:
            raise ValueError("missing_dated_universe")
        i = days.index(decision)
        cnt = Counter()
        for code, inst in sorted(universe[decision].items()):
            if (
                inst["Mkt"] not in ("0111", "0112", "0113")
                or inst["ProdCat"] != "011"
                or not code.endswith("0")
            ):
                continue
            past = [prices.get((dd, code)) for dd in days[i - 20 : i + 1]]
            f, reason = make_features(past)
            cnt[reason] += 1
            frows.append(
                {
                    "decision_date": decision,
                    "code": code,
                    "sector": inst["S33"],
                    "market": inst["Mkt"],
                    "feature_status": reason,
                    **{col: (f or {}).get(col) for col in FEATURES},
                }
            )
            for h in HORIZONS:
                entry, exit_day = days[i + 1], days[i + 1 + h]
                values = None
                why = "outcome_pending"
                if exit_day <= through:
                    leg = days[i + 1 : i + 2 + h]
                    values, why = outcome(
                        [prices.get((dd, code)) for dd in leg],
                        (index.get(entry), index.get(exit_day)),
                    )
                targets.append(
                    dict(
                        decision_date=decision,
                        code=code,
                        holding_sessions=h,
                        entry_date=entry,
                        exit_date=exit_day,
                        outcome_status=why,
                        raw_price_return=values[0] if values else None,
                        topix_price_return=values[1] if values else None,
                    )
                )
        coverage[decision] = dict(cnt)
    if not frows or len(targets) != 3 * len(frows):
        raise ValueError("row_count")
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as tmp:
        folder = Path(tmp) / "ready"
        folder.mkdir()
        files = {}
        for name, rows in (("features", frows), ("outcomes", targets)):
            t = pa.Table.from_pylist(
                rows,
                schema=None
                if name == "outcomes"
                else pa.schema(
                    [
                        ("decision_date", pa.string()),
                        ("code", pa.string()),
                        ("sector", pa.string()),
                        ("market", pa.string()),
                        ("feature_status", pa.string()),
                        *((c, pa.float64()) for c in FEATURES),
                    ]
                ),
            )
            path = folder / (name + ".parquet")
            pq.write_table(t, path)
            if not pq.read_table(path).equals(t):
                raise ValueError("parquet_mismatch")
            files[name] = {
                "rows": len(rows),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        report = dict(
            created_at=datetime.now(UTC).isoformat(),
            sample="last session in each ISO week of requested interval",
            first=first,
            last=last,
            price_cutoff=through,
            dates=coverage,
            outcomes=dict(Counter(x["outcome_status"] for x in targets)),
            horizons=HORIZONS,
            files=files,
            sources=sources,
            historical_asof_verified=False,
            execution_verified=False,
            dividends_and_fees_included=False,
        )
        (folder / "manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.rename(folder, out)
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--history", type=Path, default=Path("data/jquants/history"))
    for key in ("first", "last", "through"):
        p.add_argument("--" + key, type=date.fromisoformat, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    report = build(
        args.history,
        args.output,
        args.first.isoformat(),
        args.last.isoformat(),
        args.through.isoformat(),
    )
    print(
        json.dumps(
            {
                "dates": len(report["dates"]),
                "files": report["files"],
                "outcomes": report["outcomes"],
            }
        )
    )
