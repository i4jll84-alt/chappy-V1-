"""Read-only, exact-query snapshot coverage audit; never a historical backtest."""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import duckdb

from trading_system.ingestion.prices import price_query
from trading_system.point_in_time.policy import aware_utc
from trading_system.storage.store import canonical, digest, utcnow

SOURCES = {
    "prices": ("catalog.duckdb", "price_versions", "updated_at"),
    "calendar": ("reference/calendar/catalog.duckdb", "reference_versions", "completed_at"),
    "topix": ("market_financial/topix/catalog.duckdb", "topix_versions", "completed_at"),
}
TERMINAL = {"complete", "empty", "quality_failed"}


def _dicts(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _time(value):
    return aware_utc(value).isoformat() if value is not None else None


def _source(root, dataset, params, decision):
    relative, versions, finalized_field = SOURCES[dataset]
    result = {
        "catalog": relative,
        "dataset": dataset,
        "status": "unavailable",
        "reason": "catalog_missing",
        "job_id": None,
        "finalization_field": finalized_field,
    }
    path = root / relative
    if not path.is_file():
        return result, None
    try:
        # A read-only connection cannot create a database, migrate schema or export Parquet.
        with duckdb.connect(str(path), read_only=True) as db:
            db.execute("BEGIN TRANSACTION")
            candidates = _dicts(
                db.execute(
                    "SELECT * FROM jobs WHERE started_at<=? ORDER BY started_at DESC,job_id DESC",
                    [decision],
                )
            )
            chosen = next(
                (job for job in candidates if json.loads(job["params_json"]) == params), None
            )
            if chosen is None:
                result["reason"] = "no_exact_query_job_at_cutoff"
                return result, None
            job_id = chosen["job_id"]
            state = chosen["state"]
            finalized = chosen[finalized_field]
            result.update(
                job_id=job_id,
                state=state if state in TERMINAL | {"failed", "running", "fetched"} else "unknown",
                params=params,
                started_at=_time(chosen["started_at"]),
                updated_at=_time(chosen["updated_at"]),
                finalized_at=_time(finalized) if state in TERMINAL else None,
                rows_received=chosen["rows_received"],
                pages=chosen["pages"],
            )
            if chosen["updated_at"] > decision:
                result.update(status="indeterminate_at_cutoff", reason="status_history_not_saved")
                return result, None
            if state not in TERMINAL or chosen["cursor"] is not None:
                result.update(status="incomplete", reason="latest_attempt_not_finalized")
                return result, None
            if finalized is None or finalized + timedelta(seconds=1) > decision:
                result.update(status="not_yet_available", reason="completion_delay")
                return result, None
            quality = db.execute(
                "SELECT severity,rule,count(*) FROM quality_issues WHERE job_id=? "
                "GROUP BY severity,rule ORDER BY severity,rule",
                [job_id],
            ).fetchall()
            result["quality"] = [
                {"severity": severity, "rule": rule, "count": count}
                for severity, rule, count in quality
            ]
            result["raw_pages"] = [
                {
                    "page_number": row[0],
                    "raw_path": row[1],
                    "raw_sha256": row[2],
                    "observed_at": _time(row[3]),
                }
                for row in db.execute(
                    "SELECT page_number,raw_path,raw_sha256,observed_at FROM pages "
                    "WHERE job_id=? ORDER BY page_number",
                    [job_id],
                ).fetchall()
            ]
            key = "o.code,o.target_date" if dataset == "prices" else "o.record_key,o.target_date"
            observed = db.execute(
                "SELECT count(*),count(version_id) FROM observations WHERE job_id=?", [job_id]
            ).fetchone()
            dangling = db.execute(
                f"SELECT count(*) FROM observations o LEFT JOIN {versions} v "
                "ON o.version_id=v.version_id WHERE o.job_id=? "
                "AND o.version_id IS NOT NULL AND v.version_id IS NULL",
                [job_id],
            ).fetchone()[0]
            rows = _dicts(
                db.execute(
                    f"SELECT v.*,o.page_number,o.row_index FROM observations o "
                    f"JOIN {versions} v ON o.version_id=v.version_id WHERE o.job_id=? "
                    f"QUALIFY row_number() OVER (PARTITION BY {key} "
                    "ORDER BY o.page_number DESC,o.row_index DESC)=1 ORDER BY v.target_date",
                    [job_id],
                )
            )
            result["observations"] = observed[0]
            result["unversioned_observations"] = observed[0] - observed[1]
            result["dangling_version_references"] = dangling
            result["duplicate_observations"] = observed[1] - dangling - len(rows)
            result["versions"] = [
                {
                    "target_date": row["target_date"].isoformat(),
                    "version_id": row["version_id"],
                    "content_hash": row["content_hash"],
                    "quality_status": row["quality_status"],
                    "available_at": _time(row["available_at"]),
                    "page_number": row["page_number"],
                    "row_index": row["row_index"],
                }
                for row in rows
            ]
            result["snapshot_sha256"] = digest(
                canonical(
                    {
                        "params": params,
                        "job_id": job_id,
                        "versions": result["versions"],
                        "pages": result["raw_pages"],
                    }
                ).encode()
            )
            errors = any(item["severity"] == "error" for item in result["quality"])
            if (
                state == "quality_failed"
                or errors
                or any(row["quality_status"] != "ok" for row in rows)
            ):
                result.update(status="quality_failed", reason="snapshot_quality_errors")
                return result, None
            if any(row["available_at"] is None or row["available_at"] > decision for row in rows):
                result.update(status="not_yet_available", reason="version_unavailable_at_cutoff")
                return result, None
            if (
                observed[0] != chosen["rows_received"]
                or observed[0] != observed[1]
                or dangling
                or chosen["pages"] < 1
                or len(result["raw_pages"]) != chosen["pages"]
                or (state == "empty") != (observed[0] == 0)
            ):
                result.update(status="unavailable", reason="snapshot_integrity_mismatch")
                return result, None
            result.update(status="empty" if state == "empty" else "ready", reason=None)
            return result, rows
    except (duckdb.Error, OSError, ValueError, TypeError, KeyError):
        # Database/validator exceptions can contain payloads or credentials. No bodies escape.
        result.update(status="unavailable", reason="catalog_unreadable_or_incompatible")
        return result, None


def audit_coverage(
    root: Path, code: str, start: str, end: str, decision_at: datetime | None = None
) -> dict:
    """Save one immutable local receipt. Source databases and raw files stay untouched."""
    params = price_query(code, start, end)
    created = utcnow()
    decision = aware_utc(decision_at) if decision_at is not None else created
    root = Path(root).resolve()
    result = {
        "schema_version": 1,
        "audit_id": uuid4().hex,
        "created_at": created.isoformat(),
        "decision_at": decision.isoformat(),
        "decision_at_explicit": decision_at is not None,
        "query": params,
        "selection_policy": "exact_query_latest_started_attempt_v1",
        "availability_delay_seconds": 1,
        "limitations": [
            "Observed acquisition evidence only; no historical publication is reconstructed.",
            "Prices use terminal updated_at as completion evidence; status history is not saved.",
            "Coverage gaps are undetermined; listing and suspension status are not audited.",
            "Catalogs are read independently; selected job and version evidence is pinned.",
        ],
        "sources": {},
        "findings": [],
        "days": [],
    }
    snapshots = {}
    for dataset in SOURCES:
        query = params if dataset == "prices" else {"from": start, "to": end}
        result["sources"][dataset], snapshots[dataset] = _source(root, dataset, query, decision)

    def finding(rule, dataset, target=None):
        result["findings"].append({"rule": rule, "dataset": dataset, "date": target})

    calendar = snapshots["calendar"]
    valid_calendar = calendar is not None
    by_date = {row["target_date"]: row for row in calendar or []}
    targets = [
        date.fromisoformat(start) + timedelta(days=i)
        for i in range((date.fromisoformat(end) - date.fromisoformat(start)).days + 1)
    ]
    for target in targets:
        row = by_date.get(target)
        if calendar is not None and (
            row is None or row["holiday_division"] not in {"0", "1", "2", "3"}
        ):
            valid_calendar = False
            finding("calendar_coverage_gap", "calendar", target.isoformat())
    if not valid_calendar:
        finding("calendar_expectations_unavailable", "calendar")
    price_days = {row["target_date"]: row for row in snapshots["prices"] or []}
    topix_days = {row["target_date"]: row for row in snapshots["topix"] or []}
    for target in targets:
        calendar_row = by_date.get(target)
        expected = calendar_row["holiday_division"] in {"1", "2"} if valid_calendar else None
        day = {"date": target.isoformat(), "tse_business_day": expected}
        for dataset, records in (("prices", price_days), ("topix", topix_days)):
            row = records.get(target)
            if snapshots[dataset] is None:
                day[dataset] = "unavailable"
            elif row is None:
                day[dataset] = "absent"
                if expected:
                    finding(dataset + "_coverage_gap", dataset, target.isoformat())
            else:
                day[dataset] = "present"
                if dataset == "prices":
                    raw = json.loads(row["payload_json"])
                    if all(
                        field in raw and raw[field] is None
                        for field in ("O", "H", "L", "C", "Vo", "Va")
                    ):
                        day[dataset] = "no_trades"
                if expected is False:
                    finding("row_on_closed_day", dataset, target.isoformat())
        result["days"].append(day)
    blocked = any(
        source["status"] not in {"ready", "empty"} for source in result["sources"].values()
    )
    result["state"] = (
        "blocked" if blocked else ("coverage_failed" if result["findings"] else "clear")
    )
    folder = root / "audits" / "coverage"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (result["audit_id"] + ".json")
    result["receipt_path"] = str(path)
    with path.open("xb") as handle:
        handle.write(canonical(result).encode())
        handle.flush()
        os.fsync(handle.fileno())
    return result
