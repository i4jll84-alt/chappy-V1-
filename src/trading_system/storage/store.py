"""Single-writer DuckDB catalog with immutable raw files and recoverable Parquet snapshots."""

import hashlib
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import duckdb
import pyarrow.parquet as pq

from trading_system.point_in_time.policy import RULE, available_at, aware_utc
from trading_system.validation.prices import validate_price


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def normalized_payload(value):
    """Tag unrepresentable numbers; the original response remains untouched in Raw."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"$invalid_number": repr(value)}
    if isinstance(value, dict):
        return {key: normalized_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized_payload(item) for item in value]
    return value


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class Store:
    """Hold this connection for an entire ingestion; DuckDB excludes other writers."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        for layer in ("raw", "normalized", "processed", "logs"):
            (self.root / layer).mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self.root / "catalog.duckdb"))
        self.db.execute("SET TimeZone='UTC'")
        self.db.execute("""
            CREATE SEQUENCE IF NOT EXISTS version_sequence;
            CREATE TABLE IF NOT EXISTS jobs (
                job_id VARCHAR PRIMARY KEY, fingerprint VARCHAR, params_json VARCHAR,
                plan VARCHAR, started_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
                state VARCHAR, cursor VARCHAR, pages INTEGER DEFAULT 0,
                rows_received INTEGER DEFAULT 0, error_category VARCHAR);
            CREATE TABLE IF NOT EXISTS attempts (
                job_id VARCHAR, attempted_at TIMESTAMPTZ, status INTEGER, outcome VARCHAR);
            CREATE TABLE IF NOT EXISTS pages (
                job_id VARCHAR, page_number INTEGER, request_cursor VARCHAR,
                next_cursor VARCHAR, observed_at TIMESTAMPTZ, raw_path VARCHAR,
                raw_sha256 VARCHAR, row_count INTEGER,
                PRIMARY KEY(job_id, page_number));
            CREATE TABLE IF NOT EXISTS observations (
                job_id VARCHAR, page_number INTEGER, row_index INTEGER,
                code VARCHAR, target_date DATE, version_id BIGINT);
            CREATE TABLE IF NOT EXISTS quality_issues (
                job_id VARCHAR, page_number INTEGER, row_index INTEGER,
                severity VARCHAR, rule VARCHAR, field VARCHAR);
            CREATE TABLE IF NOT EXISTS price_versions (
                version_id BIGINT PRIMARY KEY, code VARCHAR, target_date DATE,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                volume DOUBLE, turnover DOUBLE, adjustment_factor DOUBLE,
                adjusted_open DOUBLE, adjusted_high DOUBLE, adjusted_low DOUBLE,
                adjusted_close DOUBLE, adjusted_volume DOUBLE,
                published_at TIMESTAMPTZ, observed_at TIMESTAMPTZ,
                available_at TIMESTAMPTZ, source_updated_at TIMESTAMPTZ,
                ingested_at TIMESTAMPTZ, source VARCHAR, dataset VARCHAR,
                availability_rule VARCHAR, time_confidence VARCHAR,
                content_hash VARCHAR, payload_json VARCHAR, raw_path VARCHAR,
                raw_row_index INTEGER, quality_status VARCHAR, supersedes_version_id BIGINT);
            CREATE OR REPLACE VIEW prices_current AS
                SELECT * FROM price_versions QUALIFY row_number() OVER (
                    PARTITION BY code,target_date ORDER BY version_id DESC)=1;
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def job(self, job_id: str) -> dict:
        cursor = self.db.execute("SELECT * FROM jobs WHERE job_id=?", [job_id])
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Unknown ingestion job")
        return dict(zip([c[0] for c in cursor.description], row, strict=True))

    def start(self, params: dict, plan: str, refresh: bool = False) -> dict:
        fingerprint = digest(canonical({"params": params, "plan": plan, "policy": RULE}).encode())
        if not refresh:
            found = self.db.execute(
                "SELECT job_id FROM jobs WHERE fingerprint=? ORDER BY started_at DESC LIMIT 1",
                [fingerprint],
            ).fetchone()
            if found:
                return self.job(found[0])
        job_id = uuid4().hex
        now = utcnow()
        self.db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,'running',NULL,0,0,NULL)",
            [job_id, fingerprint, canonical(params), plan, now, now],
        )
        return self.job(job_id)

    def attempt(self, job_id: str, *, status: int | None, outcome: str) -> None:
        self.db.execute(
            "INSERT INTO attempts VALUES (?,?,?,?)", [job_id, utcnow(), status, outcome]
        )

    def fail(self, job_id: str, category: str) -> None:
        self.db.execute(
            "UPDATE jobs SET state='failed',error_category=?,updated_at=? WHERE job_id=? "
            "AND state NOT IN ('complete','empty','quality_failed')",
            [category, utcnow(), job_id],
        )

    def save_page(self, job_id: str, body: dict, raw_bytes: bytes, observed_at: datetime) -> None:
        job = self.job(job_id)
        if job["state"] not in {"running", "failed"} or job["error_category"] == "export_failed":
            raise ValueError("Job is not accepting pages")
        observed_at = aware_utc(observed_at)
        params = json.loads(job["params_json"])
        page_number = job["pages"]
        next_cursor = body.get("pagination_key") or None
        # Persist even a syntactically valid response with an invalid paging cursor.
        raw_path = Path("raw") / job_id / f"{page_number:06d}-{uuid4().hex}.json"
        atomic_write(self.root / raw_path, raw_bytes)
        atomic_write(
            (self.root / raw_path).with_suffix(".meta.json"),
            canonical(
                {
                    "source": "J-Quants V2",
                    "endpoint": "equities/bars/daily",
                    "job_id": job_id,
                    "page_number": page_number,
                    "params": params,
                    "request_cursor": job["cursor"],
                    "next_cursor": next_cursor,
                    "observed_at": observed_at.isoformat(),
                    "raw_sha256": digest(raw_bytes),
                    "raw_path": raw_path.as_posix(),
                }
            ).encode(),
        )
        if next_cursor:
            seen = self.db.execute(
                "SELECT 1 FROM pages WHERE job_id=? AND request_cursor=? LIMIT 1",
                [job_id, next_cursor],
            ).fetchone()
            if next_cursor == job["cursor"] or seen:
                metadata_path = (self.root / raw_path).with_suffix(".meta.json")
                metadata = json.loads(metadata_path.read_bytes())
                metadata["rejection_reason"] = "pagination_cursor_cycle"
                atomic_write(metadata_path, canonical(metadata).encode())
                raise ValueError("Pagination cursor cycle")
        self.db.execute("BEGIN")
        try:
            for index, row in enumerate(body["data"]):
                issues = validate_price(row, expected_code=params["code"])
                target_date = None
                try:
                    target_date = date.fromisoformat(row.get("Date", ""))
                except (TypeError, ValueError):
                    pass
                if target_date and not (params["from"] <= target_date.isoformat() <= params["to"]):
                    issues.append({"severity": "error", "rule": "outside_query", "field": "Date"})
                if (
                    target_date
                    and target_date > observed_at.astimezone(ZoneInfo("Asia/Tokyo")).date()
                ):
                    issues.append({"severity": "error", "rule": "future_date", "field": "Date"})
                code = row.get("Code")
                if isinstance(code, str) and len(code) == 4:
                    code += "0"
                key_valid = target_date is not None and isinstance(code, str) and bool(code)
                version_id = None
                if key_valid:
                    payload = canonical(normalized_payload(row))
                    content_hash = digest(payload.encode())
                    old = self.db.execute(
                        "SELECT version_id,content_hash,observed_at,quality_status "
                        "FROM prices_current "
                        "WHERE code=? AND target_date=?",
                        [code, target_date],
                    ).fetchone()
                    duplicate = self.db.execute(
                        "SELECT 1 FROM observations WHERE job_id=? AND code=? AND target_date=?",
                        [job_id, code, target_date],
                    ).fetchone()
                    if duplicate:
                        # Any conflicting observation in this job keeps the key quarantined.
                        # Repeating B after A->B cannot clear the conflict; a new job can.
                        conflict = self.db.execute(
                            "SELECT 1 FROM observations o JOIN price_versions v "
                            "ON o.version_id=v.version_id WHERE o.job_id=? AND o.code=? "
                            "AND o.target_date=? AND v.content_hash<>? LIMIT 1",
                            [job_id, code, target_date, content_hash],
                        ).fetchone()
                        issues.append(
                            {
                                "severity": "error" if conflict else "warning",
                                "rule": "duplicate_conflict" if conflict else "duplicate_identical",
                                "field": "Code,Date",
                            }
                        )
                    if old and observed_at < old[2]:
                        raise ValueError("Observation time moved backwards")
                    quality_status = (
                        "error" if any(i["severity"] == "error" for i in issues) else "ok"
                    )
                    # A later validation/qualification decision is temporal evidence too.
                    if old and old[1] == content_hash and old[3] == quality_status:
                        version_id = old[0]
                    else:
                        version_id = self.db.execute(
                            "SELECT nextval('version_sequence')"
                        ).fetchone()[0]

                        def number(name):
                            value = row.get(name)
                            if isinstance(value, (int, float)) and not isinstance(value, bool):
                                try:
                                    return float(value) if math.isfinite(value) else None
                                except OverflowError:
                                    return None
                            return None

                        values = [
                            version_id,
                            code,
                            target_date,
                            *[
                                number(k)
                                for k in (
                                    "O",
                                    "H",
                                    "L",
                                    "C",
                                    "Vo",
                                    "Va",
                                    "AdjFactor",
                                    "AdjO",
                                    "AdjH",
                                    "AdjL",
                                    "AdjC",
                                    "AdjVo",
                                )
                            ],
                            None,
                            observed_at,
                            available_at(observed_at),
                            None,
                            utcnow(),
                            "J-Quants V2",
                            "equities/bars/daily",
                            RULE,
                            "observed_only",
                            content_hash,
                            payload,
                            raw_path.as_posix(),
                            index,
                            quality_status,
                            old[0] if old else None,
                        ]
                        placeholders = ",".join("?" for _ in values)
                        self.db.execute(
                            f"INSERT INTO price_versions VALUES ({placeholders})", values
                        )
                self.db.execute(
                    "INSERT INTO observations VALUES (?,?,?,?,?,?)",
                    [
                        job_id,
                        page_number,
                        index,
                        str(code) if code is not None else None,
                        target_date,
                        version_id,
                    ],
                )
                for issue in issues:
                    self.db.execute(
                        "INSERT INTO quality_issues VALUES (?,?,?,?,?,?)",
                        [
                            job_id,
                            page_number,
                            index,
                            issue["severity"],
                            issue["rule"],
                            issue["field"],
                        ],
                    )
            self.db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?)",
                [
                    job_id,
                    page_number,
                    job["cursor"],
                    next_cursor,
                    observed_at,
                    raw_path.as_posix(),
                    digest(raw_bytes),
                    len(body["data"]),
                ],
            )
            self.db.execute(
                "UPDATE jobs SET state=?,cursor=?,pages=pages+1,rows_received=rows_received+?,"
                "error_category=NULL,updated_at=? WHERE job_id=?",
                [
                    "running" if next_cursor else "fetched",
                    next_cursor,
                    len(body["data"]),
                    utcnow(),
                    job_id,
                ],
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def as_of(self, decision_at: datetime, params: dict | None = None):
        """Return one finalized query snapshot using recorded completion evidence.

        Legacy stored availability is only a row observation bound. A snapshot
        also requires the terminal job's immutable updated_at plus one second.
        Without params, select the latest finalized query across all prices.
        """
        cutoff = aware_utc(decision_at)
        args = [cutoff - timedelta(seconds=1)]
        where = "updated_at<=? AND state IN ('complete','empty','quality_failed')"
        if params is not None:
            from trading_system.ingestion.prices import price_query

            where += " AND params_json=?"
            args.append(canonical(price_query(params["code"], params["from"], params["to"])))
        args.append(cutoff)
        return self.db.execute(
            "WITH chosen AS (SELECT job_id,state,updated_at FROM jobs WHERE "
            + where
            + " ORDER BY updated_at DESC,started_at DESC,job_id DESC LIMIT 1), selected AS ("
            "SELECT v.* REPLACE (greatest(v.available_at,c.updated_at + INTERVAL '1 second') "
            "AS available_at,'job_terminal_updated_at_plus_1_second_v1' AS availability_rule),"
            "c.job_id AS snapshot_job_id,c.updated_at AS snapshot_finalized_at,"
            "'terminal_updated_at' AS finalization_source,o.page_number,o.row_index "
            "FROM observations o JOIN chosen c ON o.job_id=c.job_id AND c.state='complete' "
            "JOIN price_versions v ON o.version_id=v.version_id "
            "QUALIFY row_number() OVER (PARTITION BY o.code,o.target_date "
            "ORDER BY o.page_number DESC,o.row_index DESC)=1) "
            "SELECT * EXCLUDE(page_number,row_index) FROM selected "
            "WHERE quality_status='ok' AND available_at<=? ORDER BY code,target_date",
            args,
        ).to_arrow_table()

    def export(self) -> dict:
        """Publish an immutable generation, then atomically move the pointer last."""
        generation = uuid4().hex
        folder = self.root / "normalized" / generation
        folder.mkdir()
        tables = {
            "price_versions": "price_versions",
            "prices_current": "prices_current",
            "quality_issues": "quality_issues",
            "observations": "observations",
            "attempts": "attempts",
            "pages": "pages",
            "jobs": "jobs",
        }
        files = {}
        for name, relation in tables.items():
            table = self.db.execute(f"SELECT * FROM {relation}").to_arrow_table()
            path = folder / f"{name}.parquet"
            pq.write_table(table, path, compression="zstd")
            # Verify the actual written file through DuckDB, not just the in-memory table.
            count = self.db.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[
                0
            ]
            if count != table.num_rows:
                raise RuntimeError("Parquet verification failed")
            files[name] = {
                "path": path.relative_to(self.root).as_posix(),
                "rows": count,
                "sha256": digest(path.read_bytes()),
            }
        manifest = {
            "generation": generation,
            "created_at": utcnow().isoformat(),
            "files": files,
            "policy": RULE,
            "schema_version": 1,
        }
        atomic_write(folder / "manifest.json", canonical(manifest).encode())
        atomic_write(self.root / "normalized" / "latest.json", canonical(manifest).encode())
        return manifest

    def finish(self, job_id: str) -> dict:
        job = self.job(job_id)
        final_page = self.db.execute(
            "SELECT page_number,next_cursor,max(observed_at) OVER () FROM pages "
            "WHERE job_id=? ORDER BY page_number DESC LIMIT 1",
            [job_id],
        ).fetchone()
        if (
            not (
                job["state"] == "fetched"
                or (job["state"] == "failed" and job["error_category"] == "export_failed")
            )
            or job["cursor"] is not None
            or final_page is None
            or final_page[0] != job["pages"] - 1
            or final_page[1] is not None
        ):
            raise ValueError("Job must have a saved final page before finalization")
        errors = self.db.execute(
            "SELECT count(*) FROM quality_issues WHERE job_id=? AND severity='error'", [job_id]
        ).fetchone()[0]
        state = "quality_failed" if errors else ("complete" if job["rows_received"] else "empty")
        # Readers see completion only after both export and DB commit. A process crash can
        # leave a newer Parquet generation; the catalog remains authoritative for resuming.
        self.db.execute("BEGIN")
        try:
            self.db.execute(
                "UPDATE jobs SET state=?,error_category=NULL,updated_at=? WHERE job_id=?",
                [state, max(utcnow(), final_page[2]), job_id],
            )
            manifest = self.export()
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        receipt = {
            "job_id": job_id,
            "state": state,
            "rows_received": job["rows_received"],
            "quality_errors": errors,
            "parquet_generation": manifest["generation"],
            "verified_duckdb_read": True,
        }
        atomic_write(self.root / "logs" / f"{job_id}.json", canonical(receipt).encode())
        return receipt

    def summary(self) -> dict:
        return {
            "versions": self.db.execute("SELECT count(*) FROM price_versions").fetchone()[0],
            "current_rows": self.db.execute("SELECT count(*) FROM prices_current").fetchone()[0],
            "jobs": self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state").fetchall(),
            "quality": self.db.execute(
                "SELECT severity,rule,count(*) FROM quality_issues GROUP BY severity,rule"
            ).fetchall(),
        }
