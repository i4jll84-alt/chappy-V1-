"""Manually collect a dated cross-section after the close, using only J-Quants market data."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from trading_system.brokers.locking import single_writer
from trading_system.data_sources.jquants import JQuantsClient
from trading_system.market_financial import ingest_market_financial
from trading_system.reference import ingest_reference
from trading_system.settings import Settings, api_key
from trading_system.storage.store import atomic_write, canonical
from trading_system.validation.prices import validate_price

JST = ZoneInfo("Asia/Tokyo")


def verify_pages(folder, manifest, day, expected_codes=None):
    """Re-read the complete immutable response chain before treating the date as covered."""
    if manifest["source"] != JQuantsClient.URL or manifest["params"] != {"date": day}:
        raise ValueError("wrong_daily_request")
    codes = set()
    count = 0
    issues = Counter()
    cursors = set()
    pages = manifest["pages"]
    if not pages:
        raise ValueError("no_daily_pages")
    for n, meta in enumerate(pages, 1):
        if meta["file"] != f"page-{n:03d}.json":
            raise ValueError("unexpected_daily_path")
        body = (folder / meta["file"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != meta["sha256"]:
            raise ValueError("daily_hash_mismatch")
        obj = json.loads(body)
        rows = obj["data"]
        cursor = obj.get("pagination_key")
        if (
            len(rows) != meta["rows"]
            or bool(cursor) != (n < len(pages))
            or (cursor and cursor in cursors)
        ):
            raise ValueError("incomplete_daily_page_chain")
        if cursor:
            cursors.add(cursor)
        for row in rows:
            if row["Date"] != day or row["Code"] in codes:
                raise ValueError("invalid_daily_identity")
            codes.add(row["Code"])
            count += 1
            for issue in validate_price(row):
                issues[issue["severity"] + "/" + issue["rule"]] += 1
    if any(k.startswith("error/") for k in issues):
        raise ValueError("daily_price_quality")
    if count and expected_codes is not None and codes != expected_codes:
        raise ValueError("daily_master_code_mismatch")
    return count, dict(issues)


def snapshot(root, day, client, refresh=False, expected_codes=None):
    base = root / "daily-snapshots" / day
    if not refresh:
        for path in sorted(base.glob("*/manifest.json"), reverse=True):
            m = json.loads(path.read_bytes())
            if m.get("state") == "verified":
                count, issues = verify_pages(path.parent, m, day, expected_codes)
                if not count or count != m["rows"]:
                    raise ValueError("invalid_cached_count")
                return path, count
    folder = base / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    params = {"date": day}
    pages = []
    cursors = set()
    for n in range(1, 101):
        result = client.get_page(params)
        body = client.last_response_bytes
        now = datetime.now(UTC)
        name = f"page-{n:03d}.json"
        atomic_write(folder / name, body)
        cur = result.get("pagination_key")
        pages.append(
            dict(
                file=name,
                rows=len(result["data"]),
                sha256=hashlib.sha256(body).hexdigest(),
                observed_at=now.isoformat(),
                available_at=(now + timedelta(seconds=1)).isoformat(),
                has_next=bool(cur),
            )
        )
        if not cur:
            break
        if cur in cursors:
            raise ValueError("daily_pagination_cycle")
        cursors.add(cur)
        params = {"date": day, "pagination_key": cur}
    else:
        raise ValueError("daily_page_limit")
    m = dict(source=JQuantsClient.URL, params={"date": day}, pages=pages, source_published_at=None)
    count, issues = verify_pages(folder, m, day, expected_codes)
    m.update(state="verified" if count else "empty", rows=count, issues=issues)
    path = folder / "manifest.json"
    atomic_write(path, canonical(m).encode())
    return path, count


def collect(day, settings, refresh=False):
    now = datetime.now(JST)
    if day > now.date() or (day == now.date() and now.time() < time(16, 30)):
        raise ValueError("daily_data_after_1630_only")
    root = settings.data_root.resolve()
    iso = day.isoformat()
    secret = api_key()
    if secret is None:
        raise ValueError("market_data_key_missing")
    args = {
        "calendar": {"from": iso, "to": iso},
        "master": {"date": iso},
        "topix": {"from": iso, "to": iso},
        "financials": {"date": iso},
    }
    report = {"date": iso, "jobs": {}, "real_execution_enabled": False}
    with single_writer(root / "daily-snapshots"):
        for kind, params in args.items():
            ingest = ingest_reference if kind in ("calendar", "master") else ingest_market_financial
            with JQuantsClient(
                secret.get_secret_value(), plan=settings.plan, endpoint=kind
            ) as client:
                result = ingest(root, client, kind, params, settings.plan, refresh)
                if not refresh and result.get("state") == "empty" and kind != "financials":
                    result = ingest(root, client, kind, params, settings.plan, True)
            state = result["state"]
            report["jobs"][kind] = {k: result.get(k) for k in ("job_id", "state", "rows_received")}
            if state != "complete" and not (kind == "financials" and state == "empty"):
                raise ValueError(kind + "_not_complete")
            if kind in ("calendar", "master"):
                import duckdb

                with duckdb.connect(
                    str(root / "reference" / kind / "catalog.duckdb"), read_only=True
                ) as db:
                    pages = db.execute(
                        "SELECT raw_path,raw_sha256 FROM pages WHERE job_id=?", [result["job_id"]]
                    ).fetchall()
                records = []
                for rel, sha in pages:
                    path = (root / "reference" / kind / rel).resolve()
                    if not path.is_relative_to(root / "reference" / kind):
                        raise ValueError("unsafe_reference_path")
                    body = path.read_bytes()
                    if hashlib.sha256(body).hexdigest() != sha:
                        raise ValueError("reference_hash")
                    records.extend(json.loads(body)["data"])
                if not records or any(row["Date"] != iso for row in records):
                    raise ValueError("reference_date")
                if kind == "master":
                    codes = {row["Code"] for row in records}
                    if len(codes) != len(records):
                        raise ValueError("duplicate_master")
                elif len(records) != 1:
                    raise ValueError("calendar_count")
                elif records[0]["HolDiv"] not in ("1", "2"):
                    return {**report, "state": "closed_day"}
        with JQuantsClient(secret.get_secret_value(), plan=settings.plan) as client:
            path, count = snapshot(root, iso, client, refresh, expected_codes=codes)
        if not count:
            raise ValueError("daily_not_yet_available")
        report.update(
            state="complete",
            daily_rows=count,
            daily_manifest=str(path),
            completed_at=datetime.now(UTC).isoformat(),
        )
        atomic_write(
            root / "daily-snapshots" / iso / "last_collection.json", canonical(report).encode()
        )
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", type=date.fromisoformat, default=datetime.now(JST).date())
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()
    try:
        result = collect(args.date, Settings.load(), args.refresh)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
