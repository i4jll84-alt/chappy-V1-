"""Acquired-only TOPIX and disclosure snapshots, retaining immutable source evidence."""

import json
import math
import re
from datetime import date as calendar_date
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import duckdb
import pyarrow.parquet as pq

from trading_system.point_in_time.policy import available_at, aware_utc
from trading_system.reference import _date
from trading_system.storage.store import atomic_write, canonical, digest, normalized_payload, utcnow

RULE = "completed_query_plus_1_second_v1"
ENDPOINTS = {"topix": "indices/bars/daily/topix", "financials": "fins/summary"}
NUMBERS = ("Sales", "OP", "OdP", "NP", "EPS", "TA", "Eq", "CFO", "CFI", "CFF", "CashEq")
PERIODS = ("CurPerSt", "CurPerEn", "CurFYSt", "CurFYEn", "NxtFYSt", "NxtFYEn")
TEXT = ("DocType", "CurPerType", *PERIODS, *NUMBERS)


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9][A-Za-z0-9]{3,4}", value):
        raise ValueError("Code must be a string of 4 or 5 letters/digits")
    return value.upper() + ("0" if len(value) == 4 else "")


def market_financial_query(dataset, *, code=None, date=None, start=None, end=None):
    if dataset == "topix":
        if code is not None or date is not None:
            raise ValueError("TOPIX requires from/to only")
        params = {"from": _date(start), "to": _date(end)}
        if params["from"] > params["to"]:
            raise ValueError("Start date must not be after end date")
        return params
    if dataset == "financials":
        if start is not None or end is not None or (code is None and date is None):
            raise ValueError("Financial summary requires code and/or date, without from/to")
        params = {}
        if code is not None:
            params["code"] = _code(code)
        if date is not None:
            params["date"] = _date(date)
        return params
    raise ValueError("Dataset must be topix or financials")


def _params(dataset, params):
    allowed = {"from", "to"} if dataset == "topix" else {"code", "date"}
    if set(params) - allowed:
        raise ValueError("Unsupported market/financial query parameter")
    return market_financial_query(
        dataset,
        code=params.get("code"),
        date=params.get("date"),
        start=params.get("from"),
        end=params.get("to"),
    )


def _publication(row):
    try:
        day = _date(row.get("DiscDate"))
        time = row.get("DiscTime")
        # The documented HH:MM:00 and verified historical HH:MM responses both
        # carry minute precision. Preserve the source representation in payload.
        if not isinstance(time, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}(?::00)?", time):
            return None
        return aware_utc(
            datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=ZoneInfo("Asia/Tokyo"))
        )
    except (TypeError, ValueError):
        return None


def _topix_number(value):
    try:
        return (
            float(value)
            if isinstance(value, (float, int))
            and not isinstance(value, bool)
            and math.isfinite(value)
            else None
        )
    except (ValueError, OverflowError):
        return None


def validate_market_financial(dataset, row, params, observed_at):
    issues = []

    def add(rule, field, severity="error"):
        issues.append({"severity": severity, "rule": rule, "field": field})

    day_field = "Date" if dataset == "topix" else "DiscDate"
    try:
        day = _date(row.get(day_field))
    except (TypeError, ValueError):
        day = None
        add("invalid_date", day_field)
    if day is not None:
        if (dataset == "topix" and not params["from"] <= day <= params["to"]) or (
            "date" in params and day != params["date"]
        ):
            add("outside_query", day_field)
        if calendar_date.fromisoformat(day) > observed_at.astimezone(ZoneInfo("Asia/Tokyo")).date():
            add("future_date", day_field)
    if dataset == "topix":
        valid = {}
        for field in ("O", "H", "L", "C"):
            value = row.get(field)
            try:
                ok = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                    and value > 0
                )
            except (TypeError, OverflowError):
                ok = False
            if not ok:
                add("invalid_number", field)
            else:
                valid[field] = value
        if len(valid) == 4 and not (
            valid["L"] <= min(valid["O"], valid["C"]) <= max(valid["O"], valid["C"]) <= valid["H"]
        ):
            add("ohlc_order", "O,H,L,C")
        return issues
    try:
        code = _code(row.get("Code"))
        if "code" in params and code != params["code"]:
            add("code_mismatch", "Code")
    except ValueError:
        add("invalid_code", "Code")
    for field in ("DiscNo", "DocType"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            add("required_string", field)
    if not isinstance(row.get("DiscNo"), str) or not re.fullmatch(r"[0-9]+", row["DiscNo"]):
        add("invalid_disclosure_number", "DiscNo")
    publication = _publication(row)
    if row.get("DiscTime") in (None, ""):
        add("unknown_publication_time", "DiscTime", "warning")
    elif publication is None:
        add("invalid_publication_time", "DiscTime")
    if publication is not None and publication > observed_at:
        add("publication_after_observation", "DiscDate,DiscTime")
    for field in TEXT:
        value = row.get(field)
        if value is not None and not isinstance(value, str):
            add("expected_string", field)
    for field in PERIODS:
        if row.get(field) not in (None, ""):
            try:
                _date(row[field])
            except (TypeError, ValueError):
                add("invalid_date", field)
    for start, end in zip(PERIODS[::2], PERIODS[1::2], strict=True):
        try:
            if _date(row.get(start)) > _date(row.get(end)):
                add("period_order", f"{start},{end}")
        except (TypeError, ValueError):
            pass
    for field in NUMBERS:
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            if (
                not isinstance(value, str)
                or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value)
                or not Decimal(value).is_finite()
            ):
                raise ValueError
        except (ValueError, InvalidOperation):
            add("invalid_number", field)
    return issues


class MarketFinancialStore:
    """Single writer; new catalogs do not migrate existing Stage 1 schemas.

    Financial metrics remain validated exact decimal strings, preserving arbitrary
    source precision. An empty metric becomes null; raw/payload retain the blank.
    """

    def __init__(self, root: Path, dataset: str):
        if dataset not in ENDPOINTS:
            raise ValueError("Dataset must be topix or financials")
        self.dataset = dataset
        self.root = root.resolve() / "market_financial" / dataset
        self.root.mkdir(parents=True, exist_ok=True)
        self.versions = "topix_versions" if dataset == "topix" else "financial_versions"
        self.current = "topix_current" if dataset == "topix" else "financial_current"
        self.db = duckdb.connect(str(self.root / "catalog.duckdb"))
        self.db.execute("SET TimeZone='UTC'")
        specific = (
            "open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,"
            if dataset == "topix"
            else "code VARCHAR,disclosure_number VARCHAR,disclosure_time VARCHAR,"
            + ",".join(f'"{field}" VARCHAR' for field in TEXT)
            + ","
        )
        self.db.execute(f"""
            CREATE SEQUENCE IF NOT EXISTS record_sequence;
            CREATE TABLE IF NOT EXISTS jobs (
                job_id VARCHAR PRIMARY KEY,fingerprint VARCHAR,params_json VARCHAR,
                plan VARCHAR,started_at TIMESTAMPTZ,updated_at TIMESTAMPTZ,state VARCHAR,
                cursor VARCHAR,pages INTEGER,rows_received INTEGER,error_category VARCHAR,
                completed_at TIMESTAMPTZ);
            CREATE TABLE IF NOT EXISTS attempts (
                job_id VARCHAR,attempted_at TIMESTAMPTZ,status INTEGER,outcome VARCHAR);
            CREATE TABLE IF NOT EXISTS pages (
                job_id VARCHAR,page_number INTEGER,request_cursor VARCHAR,next_cursor VARCHAR,
                observed_at TIMESTAMPTZ,raw_path VARCHAR,raw_sha256 VARCHAR,row_count INTEGER,
                PRIMARY KEY(job_id,page_number));
            CREATE TABLE IF NOT EXISTS rejected_pages (
                job_id VARCHAR,page_number INTEGER,raw_path VARCHAR,reason VARCHAR);
            CREATE TABLE IF NOT EXISTS observations (
                job_id VARCHAR,page_number INTEGER,row_index INTEGER,
                record_key VARCHAR,target_date DATE,version_id BIGINT);
            CREATE TABLE IF NOT EXISTS quality_issues (
                job_id VARCHAR,page_number INTEGER,row_index INTEGER,
                severity VARCHAR,rule VARCHAR,field VARCHAR);
            CREATE TABLE IF NOT EXISTS {self.versions} (
                version_id BIGINT PRIMARY KEY,record_key VARCHAR,target_date DATE,{specific}
                published_at TIMESTAMPTZ,observed_at TIMESTAMPTZ,available_at TIMESTAMPTZ,
                source_updated_at TIMESTAMPTZ,ingested_at TIMESTAMPTZ,source VARCHAR,
                dataset VARCHAR,availability_rule VARCHAR,time_confidence VARCHAR,
                content_hash VARCHAR,payload_json VARCHAR,raw_path VARCHAR,raw_row_index INTEGER,
                quality_status VARCHAR,quality_fingerprint VARCHAR,
                supersedes_version_id BIGINT,first_job_id VARCHAR);
            CREATE OR REPLACE VIEW {self.current} AS SELECT * FROM {self.versions}
                QUALIFY row_number() OVER (PARTITION BY record_key ORDER BY version_id DESC)=1;
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def job(self, job_id):
        result = self.db.execute("SELECT * FROM jobs WHERE job_id=?", [job_id])
        row = result.fetchone()
        if row is None:
            raise ValueError("Unknown market/financial job")
        return dict(zip([column[0] for column in result.description], row, strict=True))

    def start(self, params, plan, refresh=False):
        params = _params(self.dataset, params)
        fingerprint = digest(canonical({"params": params, "plan": plan, "policy": RULE}).encode())
        if not refresh:
            previous = self.db.execute(
                "SELECT job_id FROM jobs WHERE fingerprint=? ORDER BY started_at DESC LIMIT 1",
                [fingerprint],
            ).fetchone()
            if previous:
                return self.job(previous[0])
        job_id, now = uuid4().hex, utcnow()
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

    def save_page(self, job_id, body, raw_bytes, observed_at):
        job = self.job(job_id)
        if job["state"] not in {"running", "failed"} or job["error_category"] == "export_failed":
            raise ValueError("Cannot append to a finalized or fetched query")
        observed_at = aware_utc(observed_at)
        params, page = json.loads(job["params_json"]), job["pages"]
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
                    "source_updated_at": None,
                    "raw_path": raw_path.as_posix(),
                    "raw_sha256": digest(raw_bytes),
                    "rejection_reason": "pagination_cursor_cycle" if cycle else None,
                }
            ).encode(),
        )
        if cycle:
            self.db.execute(
                "INSERT INTO rejected_pages VALUES (?,?,?,?)",
                [
                    job_id,
                    page,
                    raw_path.as_posix(),
                    "pagination_cursor_cycle",
                ],
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
        issues = validate_market_financial(self.dataset, row, params, observed_at)
        try:
            target = calendar_date.fromisoformat(
                _date(row.get("Date" if self.dataset == "topix" else "DiscDate"))
            )
        except (TypeError, ValueError):
            target = None
        key, code = None, None
        if self.dataset == "topix" and target is not None:
            key = canonical(["TOPIX", target.isoformat()])
        elif self.dataset == "financials":
            try:
                code = _code(row.get("Code"))
                if isinstance(row.get("DiscNo"), str) and row["DiscNo"].strip():
                    key = canonical([code, row["DiscNo"]])
            except ValueError:
                pass
        version_id = None
        if key is not None:
            payload = canonical(normalized_payload(row))
            content_hash = digest(payload.encode())
            old = self.db.execute(
                "SELECT version_id,content_hash,observed_at,quality_fingerprint "
                f"FROM {self.current} WHERE record_key=?",
                [key],
            ).fetchone()
            duplicate = self.db.execute(
                "SELECT 1 FROM observations WHERE job_id=? AND record_key=? LIMIT 1",
                [job_id, key],
            ).fetchone()
            if duplicate:
                conflict = self.db.execute(
                    f"SELECT 1 FROM observations o JOIN {self.versions} v "
                    "ON o.version_id=v.version_id "
                    "WHERE o.job_id=? AND o.record_key=? AND v.content_hash<>? LIMIT 1",
                    [job_id, key, content_hash],
                ).fetchone()
                issues.append(
                    {
                        "severity": "error" if conflict else "warning",
                        "rule": "duplicate_conflict" if conflict else "duplicate_identical",
                        "field": "record_key",
                    }
                )
            if old and observed_at < old[2]:
                raise ValueError("Observation time moved backwards")
            quality = "error" if any(issue["severity"] == "error" for issue in issues) else "ok"
            qualification = [issue for issue in issues if issue["rule"] != "duplicate_identical"]
            quality_hash = digest(canonical(qualification).encode())
            if old and old[1] == content_hash and old[3] == quality_hash:
                version_id = old[0]
            else:
                version_id = self.db.execute("SELECT nextval('record_sequence')").fetchone()[0]
                if self.dataset == "topix":
                    specific = [_topix_number(row.get(field)) for field in ("O", "H", "L", "C")]
                else:
                    specific = [
                        code,
                        row.get("DiscNo"),
                        row.get("DiscTime") if isinstance(row.get("DiscTime"), str) else None,
                    ]
                    specific += [
                        row.get(field)
                        if isinstance(row.get(field), str) and row[field] != ""
                        else None
                        for field in TEXT
                    ]
                published = _publication(row) if self.dataset == "financials" else None
                values = [
                    version_id,
                    key,
                    target,
                    *specific,
                    published,
                    observed_at,
                    None,
                    None,
                    utcnow(),
                    "J-Quants V2",
                    ENDPOINTS[self.dataset],
                    RULE,
                    "publication_minute_and_observed" if published else "observed_only",
                    content_hash,
                    payload,
                    raw_path.as_posix(),
                    index,
                    quality,
                    quality_hash,
                    old[0] if old else None,
                    job_id,
                ]
                self.db.execute(
                    f"INSERT INTO {self.versions} VALUES ({','.join('?' for _ in values)})", values
                )
        self.db.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?)",
            [job_id, page, index, key, target, version_id],
        )
        for issue in issues:
            self.db.execute(
                "INSERT INTO quality_issues VALUES (?,?,?,?,?,?)",
                [job_id, page, index, issue["severity"], issue["rule"], issue["field"]],
            )

    def as_of(self, decision_at, params=None):
        decision_at = aware_utc(decision_at)
        args = [decision_at - timedelta(seconds=1)]
        where = "completed_at<=? AND state IN ('complete','empty','quality_failed')"
        if params is not None:
            where += " AND params_json=?"
            args.append(canonical(_params(self.dataset, params)))
        args.append(decision_at)
        return self.db.execute(
            "WITH chosen AS (SELECT job_id,state FROM jobs WHERE "
            + where
            + " ORDER BY completed_at DESC,started_at DESC LIMIT 1), selected AS ("
            "SELECT v.*,o.page_number,o.row_index FROM observations o JOIN chosen c "
            "ON o.job_id=c.job_id AND c.state='complete' "
            f"JOIN {self.versions} v ON o.version_id=v.version_id "
            "QUALIFY row_number() OVER (PARTITION BY o.record_key "
            "ORDER BY o.page_number DESC,o.row_index DESC)=1) "
            "SELECT * EXCLUDE(page_number,row_index) FROM selected "
            "WHERE quality_status='ok' AND available_at<=? ORDER BY record_key",
            args,
        ).to_arrow_table()

    def summary(self):
        return {
            "versions": self.db.execute(f"SELECT count(*) FROM {self.versions}").fetchone()[0],
            "current_rows": self.db.execute(f"SELECT count(*) FROM {self.current}").fetchone()[0],
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
            self.versions,
            self.current,
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
                raise RuntimeError("Market/financial Parquet verification failed")
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
        completed = max(utcnow(), final_page[2])
        self.db.execute("BEGIN")
        try:
            self.db.execute(
                "UPDATE jobs SET state=?,error_category=NULL,updated_at=?,completed_at=? "
                "WHERE job_id=?",
                [state, completed, completed, job_id],
            )
            if state == "complete":
                self.db.execute(
                    f"UPDATE {self.versions} SET available_at=? "
                    "WHERE available_at IS NULL AND version_id IN "
                    "(SELECT version_id FROM observations WHERE job_id=?)",
                    [available_at(completed), job_id],
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


def ingest_market_financial(root, client, dataset, params, plan, refresh=False):
    params = _params(dataset, params)
    if getattr(client, "endpoint", None) != dataset:
        raise ValueError("Market/financial dataset must match client endpoint")
    with MarketFinancialStore(root, dataset) as store:
        job = store.start(params, plan, refresh)
        job_id, previous_callback = job["job_id"], client.on_attempt
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
                        if client.last_response_bytes is None:
                            raise ValueError("Original HTTP response bytes are required")
                        store.save_page(job_id, body, client.last_response_bytes, utcnow())
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
