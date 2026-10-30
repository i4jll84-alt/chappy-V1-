"""Read-only checks across archived bars, master, calendar, and TOPIX.

Passing these checks proves consistency of the received snapshot, not historical
knowledge, complete corporate actions, ordinary-share eligibility or execution.
"""

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

import duckdb

from trading_system.history_audit import safe_path
from trading_system.history_download import identity, now, sha_file
from trading_system.storage.store import atomic_write, canonical

SOURCES = {
    "prices": "/equities/bars/daily",
    "master": "/equities/master",
    "calendar": "/markets/calendar",
    "topix": "/indices/bars/daily/topix",
}
PRODUCTS = {
    "011": "内国株券（優先株等を含む）",
    "012": "優先出資証券",
    "013": "REIT",
    "014": "ETF",
    "021": "外国株券",
    "022": "外国REIT",
    "023": "外国ETF",
    "024": "外国株預託証券",
}
RAW = ("O", "H", "L", "C", "Vo", "Va")


def records(cursor):
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def number(field):
    return f"try_cast(nullif({field},'') AS DOUBLE)"


def blank(field):
    return f"({field} IS NULL OR {field}='')"


def inspect_tables(db, start=None, end=None):
    """Tables must contain strings from the original CSV, including blank cells."""
    empty_count = " + ".join(f"CASE WHEN {blank(f)} THEN 1 ELSE 0 END" for f in RAW)
    rules = {
        "invalid_date": (
            "Date IS NULL OR NOT regexp_full_match(Date,'[0-9]{4}-[0-9]{2}-[0-9]{2}')"
            " OR try_cast(Date AS DATE) IS NULL"
        ),
        "invalid_code": "Code IS NULL OR NOT regexp_full_match(Code,'[0-9][0-9A-Z]{4}')",
        "partial_empty_market_fields": f"({empty_count}) BETWEEN 1 AND 5",
        "invalid_adjustment_factor": f"{blank('AdjFactor')} OR {number('AdjFactor')}<=0",
        "ohlc_outside_range": (
            f"{number('H')}<{number('L')} OR "
            f"{number('O')} NOT BETWEEN {number('L')} AND {number('H')} OR "
            f"{number('C')} NOT BETWEEN {number('L')} AND {number('H')}"
        ),
    }
    for field in (*RAW, "AdjFactor"):
        rules["invalid_numeric_" + field] = (
            f"NOT {blank(field)} AND ({number(field)} IS NULL OR NOT isfinite({number(field)}))"
        )
    for field in RAW:
        rules["nonpositive_" + field] = f"{number(field)}<=0"
    select = ",".join(f"count(*) FILTER(WHERE {rule}) AS {name}" for name, rule in rules.items())
    checks = records(db.execute(f"SELECT {select} FROM prices"))[0]
    samples = {}
    for name, count in checks.items():
        if count:
            samples[name] = records(
                db.execute(
                    f"SELECT Date,Code,O,H,L,C,Vo,Va,AdjFactor FROM prices WHERE {rules[name]} "
                    "ORDER BY Date,Code LIMIT 10"
                )
            )
    low, high = db.execute("SELECT min(Date),max(Date) FROM prices").fetchone()
    result = {"checks": checks, "samples": samples, "start": low, "end": high}
    if low is None or high is None:
        checks["empty_prices"] = 1
        return result
    if start is not None:
        low = min(low, start.isoformat())
    if end is not None:
        high = max(high, end.isoformat())
    result.update(start=low, end=high)
    db.execute("CREATE TEMP TABLE audit_bounds(low VARCHAR,high VARCHAR)")
    db.execute("INSERT INTO audit_bounds VALUES (?,?)", [low, high])
    db.execute("CREATE TEMP VIEW price_days AS SELECT DISTINCT Date FROM prices")
    db.execute(
        "CREATE TEMP VIEW master_period AS SELECT * FROM master WHERE Date BETWEEN "
        "(SELECT low FROM audit_bounds) AND (SELECT high FROM audit_bounds)"
    )
    db.execute(
        "CREATE TEMP VIEW calendar_period AS SELECT * FROM calendar WHERE Date BETWEEN "
        "(SELECT low FROM audit_bounds) AND (SELECT high FROM audit_bounds)"
    )
    db.execute(
        "CREATE TEMP VIEW topix_period AS SELECT * FROM topix WHERE Date BETWEEN "
        "(SELECT low FROM audit_bounds) AND (SELECT high FROM audit_bounds)"
    )
    queries = {
        "duplicate_price_keys": "SELECT count(*)-count(DISTINCT (Date,Code)) FROM prices",
        "duplicate_master_keys": "SELECT count(*)-count(DISTINCT (Date,Code)) FROM master_period",
        "duplicate_calendar_dates": "SELECT count(*)-count(DISTINCT Date) FROM calendar_period",
        "duplicate_topix_dates": "SELECT count(*)-count(DISTINCT Date) FROM topix_period",
        "invalid_calendar_division": (
            "SELECT count(*) FROM calendar_period WHERE HolDiv IS NULL OR HolDiv NOT "
            "IN ('0','1','2','3')"
        ),
        "unknown_product_category": (
            "SELECT count(*) FROM master_period WHERE ProdCat IS NULL OR ProdCat NOT "
            "IN ('011','012','013','014','021','022','023','024')"
        ),
        "price_without_same_day_master": (
            "SELECT count(*) FROM prices ANTI JOIN master_period USING(Date,Code)"
        ),
        "master_without_same_day_price": (
            "SELECT count(*) FROM master_period ANTI JOIN prices USING(Date,Code)"
        ),
        "price_date_not_equity_business_day": (
            "SELECT count(*) FROM price_days p ANTI JOIN calendar_period c ON "
            "p.Date=c.Date AND c.HolDiv IN ('1','2')"
        ),
        "business_day_without_prices": (
            "SELECT count(*) FROM calendar_period c ANTI JOIN price_days p "
            "USING(Date) WHERE c.HolDiv IN ('1','2')"
        ),
        "price_date_without_topix": (
            "SELECT count(*) FROM price_days ANTI JOIN topix_period USING(Date)"
        ),
        "topix_date_without_prices": (
            "SELECT count(*) FROM topix_period ANTI JOIN price_days USING(Date)"
        ),
    }
    for name, query in queries.items():
        checks[name] = db.execute(query).fetchone()[0]
    if checks["invalid_date"] == 0:
        checks["missing_calendar_dates"] = db.execute(
            "SELECT count(*) FROM generate_series(?::DATE,?::DATE,interval 1 day) d(day) "
            "ANTI JOIN calendar_period c ON strftime(d.day,'%Y-%m-%d')=c.Date",
            [low, high],
        ).fetchone()[0]
    result["yearly"] = records(
        db.execute(
            "SELECT substr(Date,1,4) AS calendar_year,count(*) row_count,"
            "count(DISTINCT Date) sessions,"
            f"count(DISTINCT Code) codes,count(*) FILTER(WHERE ({empty_count})=6) no_trade_rows "
            "FROM prices GROUP BY calendar_year ORDER BY calendar_year"
        )
    )
    result["products"] = records(
        db.execute(
            "SELECT ProdCat,count(*) row_count,count(DISTINCT Code) codes "
            "FROM master_period GROUP BY ProdCat ORDER BY ProdCat"
        )
    )
    for row in result["products"]:
        row["label"] = PRODUCTS.get(row["ProdCat"], "unknown")
    result["last_seen_before_end"] = db.execute(
        "SELECT count(*) FROM (SELECT Code,max(Date) last_seen FROM prices GROUP BY Code) "
        "WHERE last_seen<?",
        [high],
    ).fetchone()[0]
    result["adjustment_rows"] = db.execute(
        "SELECT count(*) FROM prices WHERE try_cast(AdjFactor AS DOUBLE)<>1"
    ).fetchone()[0]
    return result


def audit_quality(root, start=None, end=None):
    root = Path(root).resolve()
    report = {
        "observed_at": now(),
        "state": "issues_found",
        "issues": [],
        "requested_start": start.isoformat() if start else None,
        "requested_end": end.isoformat() if end else None,
        "historical_point_in_time_verified": False,
        "historical_backtest_ready": False,
        "real_execution_enabled": False,
        "limitations": [
            "Snapshot consistency does not prove historical knowledge.",
            "ProdCat 011 includes preferred shares and is not an ordinary-share eligibility rule.",
            "Last seen before the end is not proof of delisting.",
            "Corporate actions, broker inventory, execution and costs are not fully verified.",
        ],
    }
    database = root / "history.sqlite"
    if not database.is_file():
        raise ValueError("history_catalog_missing")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        selected = [
            dict(r)
            for r in source.execute(
                "SELECT * FROM (SELECT *,row_number() OVER(PARTITION BY endpoint,object_key "
                "ORDER BY julianday(modified) DESC,rowid DESC) rank FROM bulk_files) "
                "WHERE rank=1 ORDER BY endpoint,object_key"
            )
            if r["endpoint"] in SOURCES.values()
        ]
    report["input_manifest"] = [
        {
            k: r[k]
            for k in (
                "id",
                "endpoint",
                "object_key",
                "modified",
                "state",
                "sha256",
                "parquet_path",
                "parquet_sha256",
                "rows",
                "available_at",
            )
        }
        for r in selected
    ]
    report["input_manifest_sha256"] = identity(report["input_manifest"])
    groups = {
        name: [r for r in selected if r["endpoint"] == endpoint]
        for name, endpoint in SOURCES.items()
    }
    for name, items in groups.items():
        if not items:
            report["issues"].append({"source": name, "problem": "missing_dataset"})
        for item in items:
            if item["state"] != "complete":
                report["issues"].append(
                    {"source": name, "problem": "latest_file_incomplete", "id": item["id"]}
                )
                continue
            for path_field, hash_field in (
                ("raw_path", "sha256"),
                ("parquet_path", "parquet_sha256"),
            ):
                path = safe_path(root, item[path_field])
                if not path.is_file() or sha_file(path) != item[hash_field]:
                    report["issues"].append(
                        {
                            "source": name,
                            "problem": "file_hash_mismatch",
                            "id": item["id"],
                            "field": path_field,
                        }
                    )
    if report["issues"]:
        return report
    with duckdb.connect() as db:
        db.execute("SET threads=4")
        for name, items in groups.items():
            db.read_parquet([str(safe_path(root, r["parquet_path"])) for r in items]).create_view(
                name
            )
        result = inspect_tables(db, start, end)
    report.update(result)
    report["issues"] = [
        {"problem": key, "count": value} for key, value in result["checks"].items() if value
    ]
    report["state"] = "issues_found" if report["issues"] else "checked"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/jquants/history"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 10, 3))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 30))
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error("invalid_date_range")
    root, output = args.root.resolve(), args.output.resolve()
    if (
        output.suffix.lower() != ".json"
        or output.exists()
        or output.is_relative_to(root / "raw")
        or output.is_relative_to(root / "normalized")
    ):
        parser.error("output must be a new JSON report outside archived evidence")
    report = audit_quality(root, args.start, args.end)
    atomic_write(output, canonical(report).encode("utf-8"))
    print(
        json.dumps({k: v for k, v in report.items() if k != "input_manifest"}, ensure_ascii=False)
    )
    return 0 if report["state"] == "checked" else 3


if __name__ == "__main__":
    raise SystemExit(main())
