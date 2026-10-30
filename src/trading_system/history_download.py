"""Resumable acquisition of official J-Quants history; never places orders.

Bulk CSV values remain strings; JSON records remain original payloads. Structural
validation is not an assertion of historical point-in-time availability.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from trading_system.brokers.locking import single_writer
from trading_system.settings import api_key
from trading_system.storage.store import atomic_write, canonical

BULK_ENDPOINTS = (
    "/equities/bars/daily",
    "/equities/master",
    "/equities/valuation",
    "/fins/summary",
    "/fins/earnings-date",
    "/equities/investor-types",
    "/indices/bars/daily/topix",
    "/indices/bars/daily",
    "/derivatives/bars/daily/options/225",
    "/markets/margin-interest",
    "/markets/short-ratio",
    "/markets/short-sale-report",
    "/markets/margin-alert",
    "/markets/calendar",
    "/derivatives/bars/daily/futures",
    "/derivatives/bars/daily/options",
    "/markets/breakdown",
    "/fins/dividend",
    "/fins/details",
    "/equities/bars/minute",
    "/equities/trades",
)
JSON_STARTS = {
    "/edinet/major-shareholders": date(2016, 6, 1),
    "/edinet/cross-shareholdings": date(2020, 3, 31),
    "/edinet/large-volume-shareholders": date(2021, 7, 1),
}
LATEST_ENDPOINT = "/equities/earnings-calendar"
API_PATHS = {"/bulk/list", "/bulk/get", LATEST_ENDPOINT, *JSON_STARTS}
STORAGE_SUFFIXES = (".r2.cloudflarestorage.com", ".amazonaws.com", ".cloudfront.net")


def now():
    return datetime.now(UTC).isoformat()


def sha_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validated_url(value):
    if not isinstance(value, str):
        raise ValueError("invalid_download_url")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or not any(parts.hostname.endswith(suffix) for suffix in STORAGE_SUFFIXES)
    ):
        raise ValueError("invalid_download_url")
    return value


class FetchError(Exception):
    def __init__(self, category, status=None):
        self.category, self.status = category, status
        super().__init__(category)


class HistoryClient:
    def __init__(self, secret, root, *, transport=None, download_transport=None, interval=0.6):
        self.root, self.interval, self.last = Path(root), interval, 0.0
        self.api = httpx.Client(
            base_url="https://api.jquants.com/v2",
            headers={"x-api-key": secret},
            trust_env=False,
            follow_redirects=False,
            timeout=40,
            transport=transport,
        )
        self.files = httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=120,
            transport=download_transport,
        )

    def close(self):
        self.api.close()
        self.files.close()

    def check_stop(self):
        if (self.root / "STOP").exists():
            raise FetchError("stop_requested")

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_stop()
            time.sleep(min(1.0, max(0, deadline - time.monotonic())))

    def get(self, endpoint, params):
        if endpoint not in API_PATHS:
            raise ValueError("endpoint_not_allowed")
        for attempt in range(4):
            self.check_stop()
            self.wait(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                response = self.api.get(endpoint, params=params)
            except httpx.TransportError:
                if attempt == 3:
                    raise FetchError("transport_error") from None
                self.wait(2**attempt)
                continue
            status = response.status_code
            if status == 429 or 500 <= status < 600:
                if attempt == 3:
                    raise FetchError("rate_or_server_error", status)
                self.wait(120 if status == 429 else 2**attempt)
                continue
            if status != 200:
                raise FetchError("http_error", status)
            try:
                payload = response.json()
            except ValueError:
                raise FetchError("malformed_json") from None
            if not isinstance(payload, dict):
                raise FetchError("malformed_json")
            return payload, response.content
        raise AssertionError("unreachable")

    def download(self, key, destination):
        payload, _ = self.get("/bulk/get", {"key": key})
        url = validated_url(payload.get("url"))
        # API credentials belong only to api.jquants.com, never the signed storage host.
        try:
            with self.files.stream("GET", url) as response:
                if response.status_code != 200:
                    raise FetchError("download_http_error", response.status_code)
                with Path(destination).open("xb") as stream:
                    for chunk in response.iter_raw():
                        self.check_stop()
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
        except httpx.TransportError:
            raise FetchError("download_transport_error") from None


def validate_rows(payload):
    rows = payload.get("data")
    cursor = payload.get("pagination_key")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid_data_array")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError("invalid_pagination_key")
    return rows, cursor


class Archive:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "work").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "history.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS discoveries(
                id TEXT PRIMARY KEY, endpoint TEXT, observed_at TEXT,
                http_status INTEGER, file_count INTEGER, bytes INTEGER, raw_path TEXT);
            CREATE TABLE IF NOT EXISTS bulk_files(
                id TEXT PRIMARY KEY, endpoint TEXT, object_key TEXT, modified TEXT,
                expected_size INTEGER, state TEXT, raw_path TEXT, sha256 TEXT,
                parquet_path TEXT, parquet_sha256 TEXT, rows INTEGER, columns_json TEXT,
                date_column TEXT, min_date TEXT, max_date TEXT, observed_at TEXT,
                available_at TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS api_jobs(
                id TEXT PRIMARY KEY, endpoint TEXT, params_json TEXT, state TEXT,
                cursor TEXT, next_page INTEGER DEFAULT 0, rows INTEGER DEFAULT 0, error TEXT);
            CREATE TABLE IF NOT EXISTS api_pages(
                job_id TEXT, page INTEGER, request_cursor TEXT, next_cursor TEXT,
                raw_path TEXT, sha256 TEXT, parquet_path TEXT, parquet_sha256 TEXT,
                rows INTEGER, observed_at TEXT, available_at TEXT,
                PRIMARY KEY(job_id,page));
            CREATE TABLE IF NOT EXISTS api_records(
                job_id TEXT, page INTEGER, row_number INTEGER, doc_id TEXT, code TEXT,
                content_hash TEXT, PRIMARY KEY(job_id,page,row_number));
        """)

    def close(self):
        self.db.close()

    def immutable(self, body, suffix=".json"):
        digest = hashlib.sha256(body).hexdigest()
        path = self.root / "raw" / digest[:2] / (digest + suffix)
        if path.exists():
            if sha_file(path) != digest:
                raise ValueError("existing_raw_corrupt")
        else:
            # Short temporary path avoids Windows MAX_PATH when filenames are hashes.
            temp = self.root / "work" / (uuid4().hex + ".part")
            with temp.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp, path)
        return path.relative_to(self.root).as_posix(), digest

    def discover(self, client, endpoint):
        try:
            payload, body = client.get("/bulk/list", {"endpoint": endpoint})
        except FetchError as exc:
            if exc.status not in (400, 403):
                raise
            with self.db:
                self.db.execute(
                    "INSERT INTO discoveries VALUES(?,?,?,?,?,?,?)",
                    (uuid4().hex, endpoint, now(), exc.status, 0, 0, None),
                )
            return
        files, cursor = validate_rows(payload)
        if cursor:
            raise ValueError("undocumented_bulk_pagination")
        raw, _ = self.immutable(body)
        checked = []
        for row in files:
            key, modified, size = row.get("Key"), row.get("LastModified"), row.get("Size")
            if (
                not isinstance(key, str)
                or not key.startswith(endpoint.lstrip("/") + "/")
                or not key.endswith(".csv.gz")
                or not isinstance(modified, str)
                or isinstance(size, bool)
                or not isinstance(size, (int, float))
                or size <= 0
                or int(size) != size
            ):
                raise ValueError("invalid_file_metadata")
            stamp = datetime.fromisoformat(modified)
            if stamp.tzinfo is None:
                raise ValueError("naive_last_modified")
            checked.append(
                (
                    identity([endpoint, key, modified, int(size)]),
                    endpoint,
                    key,
                    modified,
                    int(size),
                    "pending",
                )
            )
        with self.db:
            self.db.execute(
                "INSERT INTO discoveries VALUES(?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    endpoint,
                    now(),
                    200,
                    len(files),
                    sum(item[4] for item in checked),
                    raw,
                ),
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO bulk_files"
                "(id,endpoint,object_key,modified,expected_size,state) VALUES(?,?,?,?,?,?)",
                checked,
            )

    def normalize_csv(self, raw, file_id):
        path = self.root / raw
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream), None)
        if not header or any(not name for name in header) or len(set(header)) != len(header):
            raise ValueError("invalid_csv_header")
        # Read through gzip trailer to check CRC and uncompressed size.
        with gzip.open(path, "rb") as stream:
            while stream.read(1024 * 1024):
                pass
        target = self.root / "normalized" / (file_id[:24] + ".parquet")
        target.parent.mkdir(exist_ok=True)
        temp = self.root / "work" / (uuid4().hex + ".parquet")
        with duckdb.connect() as db:
            relation = db.sql(
                "SELECT * FROM read_csv(?, header=true, all_varchar=true, "
                "force_not_null=?, strict_mode=true)",
                params=[str(path), header],
            )
            if relation.columns != header:
                raise ValueError("csv_header_changed")
            relation.write_parquet(str(temp), compression="zstd")
            rows = pq.read_metadata(temp).num_rows
            date_column = next(
                (name for name in ("Date", "DiscDate", "PubDate") if name in header), None
            )
            low, high = None, None
            if date_column:
                low, high = db.execute(
                    f'SELECT min("{date_column}"),max("{date_column}") FROM read_parquet(?)',
                    [str(temp)],
                ).fetchone()
            csv_rows = relation.count("*").fetchone()[0]
            if rows != csv_rows:
                raise ValueError("parquet_row_count_mismatch")
        os.replace(temp, target)
        return (
            target.relative_to(self.root).as_posix(),
            sha_file(target),
            rows,
            canonical(header),
            date_column,
            low,
            high,
        )

    def fetch_bulk(self, client, item):
        destination = self.root / "work" / (uuid4().hex + ".csv.gz")
        client.download(item["object_key"], destination)
        stamp = now()
        actual = destination.stat().st_size
        digest = sha_file(destination)
        raw_path = self.root / "raw" / digest[:2] / (digest + ".csv.gz")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if raw_path.exists() and sha_file(raw_path) != digest:
            raise ValueError("existing_raw_corrupt")
        # The original downloaded bytes are retained even if listing size changed.
        if not raw_path.exists():
            os.replace(destination, raw_path)
        raw = raw_path.relative_to(self.root).as_posix()
        with self.db:
            self.db.execute(
                "UPDATE bulk_files SET raw_path=?,sha256=?,observed_at=?,state='downloaded' "
                "WHERE id=?",
                (raw, digest, stamp, item["id"]),
            )
        if actual != item["expected_size"]:
            raise ValueError("listed_size_changed")
        normalized = self.normalize_csv(raw, item["id"])
        available = (datetime.fromisoformat(now()) + timedelta(seconds=1)).isoformat()
        with self.db:
            self.db.execute(
                "UPDATE bulk_files SET state='complete',parquet_path=?,parquet_sha256=?,"
                "rows=?,columns_json=?,date_column=?,min_date=?,max_date=?,available_at=?,"
                "error=NULL WHERE id=?",
                (*normalized, available, item["id"]),
            )

    def seed_json(self, start, end):
        jobs = []
        for endpoint, earliest in JSON_STARTS.items():
            day = max(start, earliest)
            while day <= end:
                params = {"date": day.isoformat()}
                jobs.append((identity([endpoint, params]), endpoint, canonical(params), "pending"))
                day += timedelta(days=1)
        jobs.append(
            (identity([LATEST_ENDPOINT, end.isoformat()]), LATEST_ENDPOINT, "{}", "pending")
        )
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO api_jobs(id,endpoint,params_json,state) VALUES(?,?,?,?)",
                jobs,
            )

    def fetch_json_page(self, client, job):
        params = json.loads(job["params_json"])
        if job["cursor"]:
            params["pagination_key"] = job["cursor"]
        payload, body = client.get(job["endpoint"], params)
        observed = now()
        rows, cursor = validate_rows(payload)
        if cursor and (
            cursor == job["cursor"]
            or self.db.execute(
                "SELECT 1 FROM api_pages WHERE job_id=? AND request_cursor=?",
                (job["id"], cursor),
            ).fetchone()
        ):
            raise ValueError("pagination_cycle")
        raw, digest = self.immutable(body)
        records, indexes = [], []
        for index, row in enumerate(rows):
            record_hash = identity(row)
            records.append(
                {
                    "payload_json": canonical(row),
                    "row_number": index,
                    "raw_sha256": digest,
                    "observed_at": observed,
                    "source_published_at": None,
                    "source_available_at": None,
                }
            )
            indexes.append(
                (job["id"], job["next_page"], index, row.get("DocId"), row.get("Code"), record_hash)
            )
        target = self.root / "normalized" / (job["id"][:24] + f"-{job['next_page']}.parquet")
        target.parent.mkdir(exist_ok=True)
        schema = pa.schema(
            [
                ("payload_json", pa.string()),
                ("row_number", pa.int64()),
                ("raw_sha256", pa.string()),
                ("observed_at", pa.string()),
                ("source_published_at", pa.string()),
                ("source_available_at", pa.string()),
            ]
        )
        temp = self.root / "work" / (uuid4().hex + ".parquet")
        pq.write_table(pa.Table.from_pylist(records, schema=schema), temp, compression="zstd")
        if pq.read_metadata(temp).num_rows != len(rows):
            raise ValueError("parquet_row_count_mismatch")
        os.replace(temp, target)
        available = (datetime.fromisoformat(now()) + timedelta(seconds=1)).isoformat()
        with self.db:
            self.db.execute(
                "INSERT INTO api_pages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job["id"],
                    job["next_page"],
                    job["cursor"],
                    cursor,
                    raw,
                    digest,
                    target.relative_to(self.root).as_posix(),
                    sha_file(target),
                    len(rows),
                    observed,
                    available,
                ),
            )
            self.db.executemany("INSERT INTO api_records VALUES(?,?,?,?,?,?)", indexes)
            self.db.execute(
                "UPDATE api_jobs SET state=?,cursor=?,next_page=next_page+1,"
                "rows=rows+?,error=NULL WHERE id=?",
                ("running" if cursor else "complete", cursor, len(rows), job["id"]),
            )

    def status(self, phase):
        return {
            "updated_at": now(),
            "phase": phase,
            "real_execution_enabled": False,
            "bulk": [
                dict(row)
                for row in self.db.execute(
                    "SELECT endpoint,state,count(*) files,sum(expected_size) compressed_bytes,"
                    "sum(rows) rows,min(min_date) first_date,max(max_date) last_date "
                    "FROM bulk_files GROUP BY endpoint,state ORDER BY endpoint,state"
                )
            ],
            "json": [
                dict(row)
                for row in self.db.execute(
                    "SELECT endpoint,state,count(*) jobs,sum(rows) rows FROM api_jobs "
                    "GROUP BY endpoint,state ORDER BY endpoint,state"
                )
            ],
            "access": [
                dict(row)
                for row in self.db.execute(
                    "SELECT endpoint,http_status,file_count,bytes FROM discoveries "
                    "WHERE rowid IN (SELECT max(rowid) FROM discoveries GROUP BY endpoint)"
                )
            ],
            "historical_point_in_time_verified": False,
            "csv_adjusted_prices_provided": False,
        }

    def publish(self, phase):
        result = self.status(phase)
        atomic_write(self.root / "status.json", canonical(result).encode())
        return result


def run(args):
    root = args.root.resolve()
    with single_writer(root):
        archive = Archive(root)
        client = None
        try:
            if args.phase == "status":
                return archive.status("inspection")
            secret = api_key()
            if secret is None:
                raise FetchError("api_key_unavailable")
            client = HistoryClient(secret.get_secret_value(), root)
            if args.phase in ("discover", "all"):
                for endpoint in BULK_ENDPOINTS:
                    archive.discover(client, endpoint)
                    archive.publish("discovering")
                needed = archive.db.execute(
                    "SELECT coalesce(sum(expected_size),0) FROM bulk_files WHERE state!='complete'"
                ).fetchone()[0]
                if needed * 12 + 10 * 1024**3 > shutil.disk_usage(root).free:
                    raise FetchError("insufficient_disk_for_archive")
            if args.phase in ("bulk", "all"):
                # Insertion order puts daily prices first, then the other datasets.
                items = archive.db.execute(
                    "SELECT * FROM bulk_files WHERE state!='complete' ORDER BY rowid"
                ).fetchall()
                for index, item in enumerate(items):
                    try:
                        archive.fetch_bulk(client, item)
                    except (FetchError, ValueError, OSError) as exc:
                        category = (
                            exc.category if isinstance(exc, FetchError) else type(exc).__name__
                        )
                        with archive.db:
                            archive.db.execute(
                                "UPDATE bulk_files SET state='failed',error=? WHERE id=?",
                                (category, item["id"]),
                            )
                        archive.publish("needs_attention")
                        raise
                    archive.publish("bulk")
                    if index % 10 == 0:
                        print(
                            canonical(
                                {
                                    "phase": "bulk",
                                    "completed_this_run": index + 1,
                                    "remaining_this_run": len(items) - index - 1,
                                }
                            ),
                            flush=True,
                        )
                    if args.max_items and index + 1 >= args.max_items:
                        return archive.publish("batch_limit")
            if args.phase in ("json", "all"):
                archive.seed_json(args.start, args.end)
                completed = 0
                while job := archive.db.execute(
                    "SELECT * FROM api_jobs WHERE state!='complete' "
                    "ORDER BY params_json DESC,endpoint LIMIT 1"
                ).fetchone():
                    try:
                        archive.fetch_json_page(client, job)
                    except FetchError as exc:
                        with archive.db:
                            archive.db.execute(
                                "UPDATE api_jobs SET state='failed',error=? WHERE id=?",
                                (exc.category + ":" + str(exc.status), job["id"]),
                            )
                        archive.publish("needs_attention")
                        raise
                    completed += 1
                    archive.publish("json")
                    if completed % 50 == 0:
                        print(canonical({"phase": "json", "pages_this_run": completed}), flush=True)
                    if args.max_items and completed >= args.max_items:
                        return archive.publish("batch_limit")
            return archive.publish("complete")
        except BaseException:
            archive.publish("interrupted_or_failed")
            raise
        finally:
            if client:
                client.close()
            archive.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/jquants/history"))
    parser.add_argument(
        "--phase", choices=("discover", "bulk", "json", "all", "status"), default="all"
    )
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 10, 31))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 30))
    parser.add_argument("--max-items", type=int, default=0)
    args = parser.parse_args(argv)
    if args.start > args.end or args.max_items < 0:
        parser.error("invalid date range or batch size")
    try:
        result = run(args)
    except Exception as exc:
        print(
            canonical(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "category": exc.category if isinstance(exc, FetchError) else "local_validation",
                    "http_status": exc.status if isinstance(exc, FetchError) else None,
                }
            ),
            flush=True,
        )
        return 2
    print(canonical(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
