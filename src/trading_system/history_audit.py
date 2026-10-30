"""Read-only integrity and daily-price checks for the separate history archive."""

import argparse
import json
import sqlite3
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from trading_system.history_download import now, sha_file
from trading_system.storage.store import atomic_write, canonical


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("archive_path_outside_root")
    return path


def audit_history(root):
    root = Path(root).resolve()
    database = root / "history.sqlite"
    if not database.is_file():
        raise ValueError("history_catalog_missing")
    report = {
        "observed_at": now(),
        "state": "checked",
        "issues": [],
        "files_checked": 0,
        "pages_checked": 0,
        "historical_point_in_time_verified": False,
        "real_execution_enabled": False,
    }
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        completed = [
            dict(row) for row in db.execute("SELECT * FROM bulk_files WHERE state='complete'")
        ]
        active = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM (SELECT *,row_number() OVER(PARTITION BY endpoint,object_key "
                "ORDER BY julianday(modified) DESC,rowid DESC) rank FROM bulk_files) "
                "WHERE rank=1 AND state='complete'"
            )
        ]
        pages = [dict(row) for row in db.execute("SELECT * FROM api_pages")]
        report["bulk_states"] = [
            dict(row)
            for row in db.execute(
                "SELECT endpoint,state,count(*) files,sum(rows) rows,min(min_date) first_date,"
                "max(max_date) last_date FROM bulk_files GROUP BY endpoint,state"
            )
        ]
        report["api_states"] = [
            dict(row)
            for row in db.execute(
                "SELECT endpoint,state,count(*) jobs,sum(rows) rows FROM api_jobs "
                "GROUP BY endpoint,state"
            )
        ]
        report["record_lineage_difference"] = db.execute(
            "SELECT abs((SELECT coalesce(sum(rows),0) FROM api_pages)-"
            "(SELECT count(*) FROM api_records))"
        ).fetchone()[0]
    for kind, items in (("files", completed), ("pages", pages)):
        for item in items:
            label = item.get("id") or f"{item['job_id']}:{item['page']}"
            for field, hash_field in (("raw_path", "sha256"), ("parquet_path", "parquet_sha256")):
                path = safe_path(root, item[field])
                if not path.is_file() or sha_file(path) != item[hash_field]:
                    report["issues"].append(
                        {"id": label, "kind": field, "problem": "hash_or_missing"}
                    )
            path = safe_path(root, item["parquet_path"])
            if path.is_file():
                try:
                    if pq.read_metadata(path).num_rows != item["rows"]:
                        report["issues"].append({"id": label, "problem": "row_count"})
                except Exception:
                    report["issues"].append({"id": label, "problem": "parquet_unreadable"})
            report[kind + "_checked"] += 1
    if report["record_lineage_difference"]:
        report["issues"].append({"problem": "record_lineage"})
    prices = [
        str(safe_path(root, item["parquet_path"]))
        for item in active
        if item["endpoint"] == "/equities/bars/daily"
    ]
    if prices and not report["issues"]:
        with duckdb.connect() as db:
            db.read_parquet(prices).create_view("daily")
            # Counts refer to received data; absent pre-listing/post-delisting rows
            # are not silently interpreted as missing trading days.
            result = db.execute("""
                SELECT count(*), count(DISTINCT Code), count(DISTINCT Date),
                min(Date), max(Date),
                count(*) - count(DISTINCT (Date,Code)),
                count(*) FILTER (WHERE try_cast(Date AS DATE) IS NULL),
                count(*) FILTER (WHERE O='' AND H='' AND L='' AND C='' AND Vo='' AND Va=''),
                count(*) FILTER (WHERE
                    try_cast(nullif(O,'') AS DOUBLE)<=0 OR
                    try_cast(nullif(H,'') AS DOUBLE)<=0 OR
                    try_cast(nullif(L,'') AS DOUBLE)<=0 OR
                    try_cast(nullif(C,'') AS DOUBLE)<=0 OR
                    try_cast(nullif(Vo,'') AS DOUBLE)<0 OR
                    try_cast(nullif(Va,'') AS DOUBLE)<0 OR
                    try_cast(nullif(H,'') AS DOUBLE)<try_cast(nullif(L,'') AS DOUBLE))
                FROM daily
            """).fetchone()
            report["daily_prices"] = dict(
                zip(
                    (
                        "rows",
                        "codes",
                        "dates",
                        "first_date",
                        "last_date",
                        "duplicate_code_dates",
                        "invalid_dates",
                        "no_trade_rows",
                        "invalid_price_volume_or_range",
                    ),
                    result,
                    strict=True,
                )
            )
            report["daily_prices"]["scope"] = "received_files_only_not_pit_or_universe_proof"
    for key in ("duplicate_code_dates", "invalid_dates", "invalid_price_volume_or_range"):
        if report.get("daily_prices", {}).get(key, 0):
            report["issues"].append(
                {"problem": "daily_" + key, "count": report["daily_prices"][key]}
            )
    report["state"] = "issues_found" if report["issues"] else "checked"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/jquants/history"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root, target = args.root.resolve(), args.output.resolve()
    if (
        target == root / "history.sqlite"
        or target.is_relative_to(root / "raw")
        or target.is_relative_to(root / "normalized")
    ):
        parser.error("output must not replace archived evidence")
    result = audit_history(root)
    atomic_write(target, canonical(result).encode())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["state"] == "checked" else 3


if __name__ == "__main__":
    raise SystemExit(main())
