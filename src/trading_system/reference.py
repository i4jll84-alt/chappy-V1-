"""Auditable daily master/calendar snapshots; no historical publication is inferred."""

import json
import re
from datetime import date as calendar_date
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import duckdb
import pyarrow.parquet as pq

from trading_system.point_in_time.policy import RULE, available_at, aware_utc
from trading_system.storage.store import (
    atomic_write,
    canonical,
    digest,
    normalized_payload,
    utcnow,
)

ENDPOINTS = {"master": "equities/master", "calendar": "markets/calendar"}


def _date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use explicit YYYY-MM-DD dates")
    calendar_date.fromisoformat(value)
    return value


def reference_query(dataset, date=None, code=None, start=None, end=None) -> dict[str, str]:
    """Require bounded, repeatable queries even where the API permits omitted dates."""
    if dataset == "master":
        if start is not None or end is not None:
            raise ValueError("Master requires date, not a date range")
        params = {"date": _date(date)}
        if code is not None:
            if not isinstance(code, str) or not re.fullmatch(r"[0-9][A-Za-z0-9]{3,4}", code):
                raise ValueError("Code must be a string of 4 or 5 letters/digits")
            params["code"] = code.upper() + ("0" if len(code) == 4 else "")
        return params
    if dataset == "calendar":
        if date is not None or code is not None:
            raise ValueError("Calendar requires from/to and does not accept a code")
        params = {"from": _date(start), "to": _date(end)}
        if params["from"] > params["to"]:
            raise ValueError("Start date must not be after end date")
        return params
    raise ValueError("Dataset must be master or calendar")


def _params(dataset: str, params: dict) -> dict[str, str]:
    allowed = {"date", "code"} if dataset == "master" else {"from", "to"}
    if set(params) - allowed:
        raise ValueError("Unsupported reference query parameter")
    return reference_query(
        dataset,
        date=params.get("date"),
        code=params.get("code"),
        start=params.get("from"),
        end=params.get("to"),
    )


def validate_reference(dataset: str, row: dict, params: dict) -> list[dict]:
    issues = []

    def issue(rule, field, severity="error"):
        issues.append({"severity": severity, "rule": rule, "field": field})

    try:
        target = _date(row.get("Date"))
    except (TypeError, ValueError):
        target = None
        issue("invalid_date", "Date")
    if dataset == "master":
        for field in ("Code", "CoName", "Mkt", "S17", "S33", "ProdCat"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                issue("required_string", field)
        code = row.get("Code")
        if row.get("ProdCat") not in ("011", "012", "013", "014", "021", "022", "023", "024"):
            issue("invalid_product_category", "ProdCat")
        if isinstance(code, str) and not re.fullmatch(r"[0-9][A-Z0-9]{3,4}", code):
            issue("invalid_code", "Code")
        if "code" in params and isinstance(code, str):
            normalized_code = code + ("0" if len(code) == 4 else "")
            if normalized_code != params["code"]:
                issue("code_mismatch", "Code")
        if target is not None and target != params["date"]:
            # Holidays, out-of-range old dates and future dates can legitimately map
            # to another snapshot. The mapping needs separate calendar verification.
            issue("snapshot_date_mapping_unverified", "Date", "warning")
    else:
        if row.get("HolDiv") not in ("0", "1", "2", "3"):
            issue("invalid_holiday_division", "HolDiv")
        if target is not None and not params["from"] <= target <= params["to"]:
            issue("outside_query", "Date")
    return issues


class ReferenceStore:
    """Single writer per reference dataset; the catalog is authoritative on recovery."""

    def __init__(self, root: Path, dataset: str):
        if dataset not in ENDPOINTS:
            raise ValueError("Dataset must be master or calendar")
        self.dataset = dataset
        self.root = root.resolve() / "reference" / dataset
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self.root / "catalog.duckdb"))
        self.db.execute("SET TimeZone='UTC'")
        self.db.execute("""
            CREATE SEQUENCE IF NOT EXISTS reference_sequence;
            CREATE TABLE IF NOT EXISTS jobs (
                job_id VARCHAR PRIMARY KEY, fingerprint VARCHAR, params_json VARCHAR,
                plan VARCHAR, started_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
                state VARCHAR, cursor VARCHAR, pages INTEGER, rows_received INTEGER,
                error_category VARCHAR, completed_at TIMESTAMPTZ);
            CREATE TABLE IF NOT EXISTS attempts (
                job_id VARCHAR, attempted_at TIMESTAMPTZ, status INTEGER, outcome VARCHAR);
            CREATE TABLE IF NOT EXISTS pages (
                job_id VARCHAR, page_number INTEGER, request_cursor VARCHAR,
                next_cursor VARCHAR, observed_at TIMESTAMPTZ, raw_path VARCHAR,
                raw_sha256 VARCHAR, row_count INTEGER, PRIMARY KEY(job_id,page_number));
            CREATE TABLE IF NOT EXISTS rejected_pages (
                job_id VARCHAR, page_number INTEGER, raw_path VARCHAR, reason VARCHAR);
            CREATE TABLE IF NOT EXISTS observations (
                job_id VARCHAR, page_number INTEGER, row_index INTEGER,
                record_key VARCHAR, target_date DATE, version_id BIGINT);
            CREATE TABLE IF NOT EXISTS quality_issues (
                job_id VARCHAR, page_number INTEGER, row_index INTEGER,
                severity VARCHAR, rule VARCHAR, field VARCHAR);
            CREATE TABLE IF NOT EXISTS reference_versions (
                version_id BIGINT PRIMARY KEY, record_key VARCHAR, target_date DATE,
                code VARCHAR, company_name VARCHAR, market VARCHAR, sector17 VARCHAR,
                sector33 VARCHAR, product_category VARCHAR, holiday_division VARCHAR,
                is_tse_business_day BOOLEAN,
                published_at TIMESTAMPTZ, observed_at TIMESTAMPTZ,
                available_at TIMESTAMPTZ, source_updated_at TIMESTAMPTZ,
                ingested_at TIMESTAMPTZ, source VARCHAR, dataset VARCHAR,
                availability_rule VARCHAR, time_confidence VARCHAR,
                content_hash VARCHAR, payload_json VARCHAR, raw_path VARCHAR,
                raw_row_index INTEGER, quality_status VARCHAR, quality_fingerprint VARCHAR,
                supersedes_version_id BIGINT);
            CREATE OR REPLACE VIEW reference_current AS
                SELECT * FROM reference_versions QUALIFY row_number() OVER (
                    PARTITION BY record_key,target_date ORDER BY version_id DESC)=1;
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def job(self, job_id: str) -> dict:
        cursor = self.db.execute("SELECT * FROM jobs WHERE job_id=?", [job_id])
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Unknown reference job")
        return dict(zip([column[0] for column in cursor.description], row, strict=True))

    def start(self, params: dict, plan: str, refresh=False) -> dict:
        params = _params(self.dataset, params)
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
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,'running',NULL,0,0,NULL,NULL)",
            [job_id, fingerprint, canonical(params), plan, now, now],
        )
        return self.job(job_id)

    def attempt(self, job_id, *, status, outcome):
        self.db.execute(
            "INSERT INTO attempts VALUES (?,?,?,?)", [job_id, utcnow(), status, outcome]
        )

    def fail(self, job_id, category):
        self.db.execute(
            "UPDATE jobs SET state='failed',error_category=?,updated_at=? WHERE job_id=? "
            "AND state NOT IN ('complete','empty','quality_failed')",
            [category, utcnow(), job_id],
        )

    def save_page(self, job_id, body, raw_bytes: bytes, observed_at: datetime):
        job = self.job(job_id)
        if job["state"] not in {"running", "failed"} or job["error_category"] == "export_failed":
            raise ValueError("Job is not accepting pages")
        observed_at = aware_utc(observed_at)
        params = json.loads(job["params_json"])
        page = job["pages"]
        next_cursor = body.get("pagination_key") or None
        cycle = bool(next_cursor) and (
            next_cursor == job["cursor"]
            or self.db.execute(
                "SELECT 1 FROM pages WHERE job_id=? AND request_cursor=? LIMIT 1",
                [job_id, next_cursor],
            ).fetchone()
            is not None
        )
        raw_path = Path("raw") / job_id / f"{page:06d}-{uuid4().hex}.json"
        atomic_write(self.root / raw_path, raw_bytes)
        atomic_write(
            (self.root / raw_path).with_suffix(".meta.json"),
            canonical(
                {
                    "source": "J-Quants V2",
                    "endpoint": ENDPOINTS[self.dataset],
                    "job_id": job_id,
                    "page_number": page,
                    "params": params,
                    "request_cursor": job["cursor"],
                    "next_cursor": next_cursor,
                    "observed_at": observed_at.isoformat(),
                    "published_at": None,
                    "source_updated_at": None,
                    "raw_sha256": digest(raw_bytes),
                    "raw_path": raw_path.as_posix(),
                    "rejection_reason": "pagination_cursor_cycle" if cycle else None,
                }
            ).encode(),
        )
        if cycle:
            self.db.execute(
                "INSERT INTO rejected_pages VALUES (?,?,?,?)",
                [job_id, page, raw_path.as_posix(), "pagination_cursor_cycle"],
            )
            raise ValueError("Pagination cursor cycle")
        self.db.execute("BEGIN")
        try:
            for index, row in enumerate(body["data"]):
                self._save_row(job_id, page, index, row, params, raw_path, observed_at)
            self.db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?)",
                [
                    job_id,
                    page,
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

    def _save_row(self, job_id, page, index, row, params, raw_path, observed_at):
        issues = validate_reference(self.dataset, row, params)
        try:
            target = calendar_date.fromisoformat(_date(row.get("Date")))
        except (TypeError, ValueError):
            target = None
        code = row.get("Code") if self.dataset == "master" else None
        if isinstance(code, str) and len(code) == 4:
            code += "0"
        key = code if self.dataset == "master" else "calendar"
        version_id = None
        if target is not None and isinstance(key, str) and key:
            payload = canonical(normalized_payload(row))
            content_hash = digest(payload.encode())
            old = self.db.execute(
                "SELECT version_id,content_hash,observed_at,quality_fingerprint "
                "FROM reference_current WHERE record_key=? AND target_date=?",
                [key, target],
            ).fetchone()
            duplicate = self.db.execute(
                "SELECT 1 FROM observations WHERE job_id=? AND record_key=? AND target_date=?",
                [job_id, key, target],
            ).fetchone()
            if duplicate:
                conflict = self.db.execute(
                    "SELECT 1 FROM observations o JOIN reference_versions v "
                    "ON o.version_id=v.version_id WHERE o.job_id=? AND o.record_key=? "
                    "AND o.target_date=? AND v.content_hash<>? LIMIT 1",
                    [job_id, key, target, content_hash],
                ).fetchone()
                issues.append(
                    {
                        "severity": "error" if conflict else "warning",
                        "rule": "duplicate_conflict" if conflict else "duplicate_identical",
                        "field": "Code,Date" if code is not None else "Date",
                    }
                )
            if old and observed_at < old[2]:
                raise ValueError("Observation time moved backwards")
            quality = "error" if any(i["severity"] == "error" for i in issues) else "ok"
            # Identical-repeat warnings describe observations, not a new qualification.
            qualification = [i for i in issues if i["rule"] != "duplicate_identical"]
            quality_hash = digest(canonical(qualification).encode())
            if old and old[1] == content_hash and old[3] == quality_hash:
                version_id = old[0]
            else:
                version_id = self.db.execute("SELECT nextval('reference_sequence')").fetchone()[0]

                def text(field):
                    return row.get(field) if isinstance(row.get(field), str) else None

                values = [
                    version_id,
                    key,
                    target,
                    code if isinstance(code, str) else None,
                    text("CoName"),
                    text("Mkt"),
                    text("S17"),
                    text("S33"),
                    text("ProdCat"),
                    text("HolDiv"),
                    (row["HolDiv"] in ("1", "2"))
                    if self.dataset == "calendar" and row.get("HolDiv") in ("0", "1", "2", "3")
                    else None,
                    None,
                    observed_at,
                    available_at(observed_at),
                    None,
                    utcnow(),
                    "J-Quants V2",
                    ENDPOINTS[self.dataset],
                    RULE,
                    "observed_only",
                    content_hash,
                    payload,
                    raw_path.as_posix(),
                    index,
                    quality,
                    quality_hash,
                    old[0] if old else None,
                ]
                self.db.execute(
                    f"INSERT INTO reference_versions VALUES ({','.join('?' for _ in values)})",
                    values,
                )
        self.db.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?)",
            [job_id, page, index, key if isinstance(key, str) else None, target, version_id],
        )
        for issue in issues:
            self.db.execute(
                "INSERT INTO quality_issues VALUES (?,?,?,?,?,?)",
                [job_id, page, index, issue["severity"], issue["rule"], issue["field"]],
            )

    def as_of(self, decision_at: datetime, params: dict | None = None):
        """Return ONE completed query snapshot, never a union of stale master rows.

        With params, choose the latest finalized job for that exact query. Without
        params, choose the latest finalized query in this dataset. Invalid latest
        rows are excluded without falling back to an older clean snapshot.
        """
        cutoff = aware_utc(decision_at) - timedelta(seconds=1)
        where = "completed_at<=? AND state IN ('complete','empty','quality_failed')"
        args = [cutoff]
        if params is not None:
            where += " AND params_json=?"
            args.append(canonical(_params(self.dataset, params)))
        return self.db.execute(
            "WITH chosen AS (SELECT job_id,state FROM jobs WHERE "
            + where
            + " ORDER BY completed_at DESC,started_at DESC LIMIT 1), selected AS ("
            "SELECT v.*,o.page_number,o.row_index FROM observations o "
            "JOIN chosen c ON o.job_id=c.job_id AND c.state='complete' JOIN reference_versions v "
            "ON o.version_id=v.version_id QUALIFY row_number() OVER ("
            "PARTITION BY o.record_key,o.target_date ORDER BY o.page_number DESC,"
            "o.row_index DESC)=1) SELECT * EXCLUDE(page_number,row_index) FROM selected "
            "WHERE quality_status='ok' ORDER BY record_key,target_date",
            args,
        ).to_arrow_table()

    def summary(self):
        return {
            "versions": self.db.execute("SELECT count(*) FROM reference_versions").fetchone()[0],
            "current_rows": self.db.execute("SELECT count(*) FROM reference_current").fetchone()[0],
            "jobs": self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state").fetchall(),
            "quality": self.db.execute(
                "SELECT severity,rule,count(*) FROM quality_issues GROUP BY severity,rule"
            ).fetchall(),
        }

    def export(self):
        generation = uuid4().hex
        folder = self.root / "normalized" / generation
        folder.mkdir(parents=True)
        files = {}
        for relation in (
            "reference_versions",
            "reference_current",
            "observations",
            "quality_issues",
            "pages",
            "rejected_pages",
            "attempts",
            "jobs",
        ):
            table = self.db.execute(f"SELECT * FROM {relation}").to_arrow_table()
            path = folder / f"{relation}.parquet"
            pq.write_table(table, path, compression="zstd")
            count = self.db.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[
                0
            ]
            if count != table.num_rows:
                raise RuntimeError("Reference Parquet verification failed")
            files[relation] = {
                "path": path.relative_to(self.root).as_posix(),
                "rows": count,
                "sha256": digest(path.read_bytes()),
            }
        manifest = {
            "generation": generation,
            "dataset": self.dataset,
            "created_at": utcnow().isoformat(),
            "files": files,
            "policy": RULE,
            "schema_version": 1,
        }
        atomic_write(folder / "manifest.json", canonical(manifest).encode())
        atomic_write(self.root / "normalized" / "latest.json", canonical(manifest).encode())
        return manifest

    def finish(self, job_id):
        """completed_at means all pages and quality checks saved, before Parquet export.

        The terminal state and completion time become visible with the DB commit.
        Cached jobs always rebuild Parquet, so an interrupted pointer publication
        can never cause a cached request to trust a generation ahead of the catalog.
        """
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
        params = json.loads(job["params_json"])
        if (
            self.dataset == "master"
            and self.db.execute(
                "SELECT count(DISTINCT target_date) FROM observations WHERE job_id=?", [job_id]
            ).fetchone()[0]
            > 1
        ):
            if not self.db.execute(
                "SELECT 1 FROM quality_issues WHERE job_id=? AND rule='snapshot_mixed_dates'",
                [job_id],
            ).fetchone():
                self.db.execute(
                    "INSERT INTO quality_issues VALUES (?,-1,-1,'error',"
                    "'snapshot_mixed_dates','Date')",
                    [job_id],
                )
        if self.dataset == "calendar" and job["rows_received"]:
            actual = {
                row[0]
                for row in self.db.execute(
                    "SELECT DISTINCT target_date FROM observations WHERE job_id=? "
                    "AND target_date IS NOT NULL",
                    [job_id],
                ).fetchall()
            }
            start = calendar_date.fromisoformat(params["from"])
            end = calendar_date.fromisoformat(params["to"])
            missing = any(
                start + timedelta(days=i) not in actual for i in range((end - start).days + 1)
            )
            if (
                missing
                and not self.db.execute(
                    "SELECT 1 FROM quality_issues WHERE job_id=? AND rule='missing_calendar_dates'",
                    [job_id],
                ).fetchone()
            ):
                self.db.execute(
                    "INSERT INTO quality_issues VALUES (?,-1,-1,'error',"
                    "'missing_calendar_dates','Date')",
                    [job_id],
                )
        errors = self.db.execute(
            "SELECT count(*) FROM quality_issues WHERE job_id=? AND severity='error'", [job_id]
        ).fetchone()[0]
        state = "quality_failed" if errors else ("complete" if job["rows_received"] else "empty")
        completed = max(utcnow(), final_page[2])
        self.db.execute("BEGIN")
        try:
            self.db.execute(
                "UPDATE jobs SET state=?,error_category=NULL,updated_at=?,completed_at=? "
                "WHERE job_id=?",
                [state, completed, completed, job_id],
            )
            manifest = self.export()
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        receipt = {
            "job_id": job_id,
            "dataset": self.dataset,
            "state": state,
            "rows_received": job["rows_received"],
            "quality_errors": errors,
            "parquet_generation": manifest["generation"],
            "verified_duckdb_read": True,
            **self.summary(),
        }
        atomic_write(self.root / "logs" / f"{job_id}.json", canonical(receipt).encode())
        return receipt


def ingest_reference(root: Path, client, dataset: str, params: dict, plan: str, refresh=False):
    params = _params(dataset, params)
    if getattr(client, "endpoint", None) != dataset:
        raise ValueError("Reference dataset must match the client endpoint")
    with ReferenceStore(root, dataset) as store:
        job = store.start(params, plan, refresh)
        job_id = job["job_id"]
        previous_callback = client.on_attempt
        client.on_attempt = lambda **event: store.attempt(job_id, **event)
        try:
            if job["state"] in {"complete", "empty", "quality_failed"}:
                manifest = store.export()
                return {
                    "job_id": job_id,
                    "dataset": dataset,
                    "state": job["state"],
                    "cached": True,
                    "rows_received": job["rows_received"],
                    "parquet_generation": manifest["generation"],
                    "verified_duckdb_read": True,
                    **store.summary(),
                }
            try:
                if job["state"] != "fetched" and job["error_category"] != "export_failed":
                    for _ in range(10000):
                        request = dict(params)
                        if job["cursor"]:
                            request["pagination_key"] = job["cursor"]
                        body = client.get_page(request)
                        observed_at = utcnow()
                        if client.last_response_bytes is None:
                            raise ValueError("Original HTTP response bytes are required")
                        store.save_page(job_id, body, client.last_response_bytes, observed_at)
                        job = store.job(job_id)
                        if job["state"] == "fetched":
                            break
                    else:
                        raise ValueError("Pagination page limit exceeded")
            except Exception as exc:
                store.fail(job_id, getattr(exc, "category", "ingestion_failed"))
                raise
            try:
                return store.finish(job_id)
            except Exception:
                store.fail(job_id, "export_failed")
                raise
        finally:
            client.on_attempt = previous_callback
