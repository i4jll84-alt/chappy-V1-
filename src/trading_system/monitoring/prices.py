"""Read-only latest-attempt observations of saved J-Quants price acquisitions."""

import hashlib
import json
import math
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import duckdb

from trading_system.ingestion.prices import price_query
from trading_system.point_in_time.policy import aware_utc
from trading_system.storage.store import canonical

STATES = {"running", "fetched", "failed", "complete", "empty", "quality_failed"}
TERMINAL = {"complete", "empty", "quality_failed"}
CATEGORIES = {
    "success",
    "transport_error",
    "rate_limited",
    "server_error",
    "invalid_request_or_period",
    "authentication_failed",
    "access_denied",
    "http_error",
    "malformed_payload",
    "ingestion_failed",
    "export_failed",
}
SCHEMA = {
    "jobs": (
        "job_id,params_json,plan,started_at,updated_at,state,cursor,"
        "pages,rows_received,error_category"
    ),
    "pages": "page_number,request_cursor,next_cursor,observed_at,raw_sha256,row_count",
    "observations": "page_number,row_index,code,target_date,version_id",
    "quality_issues": "page_number,row_index,severity,rule",
    "attempts": "attempted_at,status,outcome",
    "price_versions": "version_id,code,target_date,observed_at,quality_status,content_hash",
}


def _require(condition):
    if not condition:
        raise ValueError("Invalid price monitoring evidence")


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _params(value):
    payload = json.loads(value, object_pairs_hook=_pairs)
    _require(type(payload) is dict and set(payload) == {"code", "from", "to"})
    normalized = price_query(payload["code"], payload["from"], payload["to"])
    _require(payload == normalized)
    return normalized


def _rows(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _encoded(value):
    if isinstance(value, datetime):
        return aware_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _encoded(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encoded(item) for item in value]
    return value


def _hash(value):
    return hashlib.sha256(canonical(_encoded(value)).encode()).hexdigest()


def _snapshot(database, params):
    with duckdb.connect(str(database), read_only=True) as connection:
        connection.execute("BEGIN TRANSACTION")
        tables = dict(
            connection.execute(
                "SELECT table_name,table_type FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name IN (?,?,?,?,?,?)",
                list(SCHEMA),
            ).fetchall()
        )
        _require(tables == dict.fromkeys(SCHEMA, "BASE TABLE"))
        for table, fields in SCHEMA.items():
            # Select only the supported schema; no initializer, views or migrations.
            prefix = (
                "job_id,"
                if table in {"pages", "observations", "quality_issues", "attempts"}
                else ""
            )
            connection.execute(f"SELECT {prefix}{fields} FROM {table} LIMIT 0")
        jobs = _rows(
            connection.execute(
                f"SELECT {SCHEMA['jobs']} FROM jobs ORDER BY started_at DESC,job_id DESC"
            )
        )
        matching = [job for job in jobs if _params(job["params_json"]) == params]
        chosen = matching[0] if matching else None
        snapshot = {"matching_jobs": matching, "chosen": chosen}
        if chosen:
            job_id = chosen["job_id"]
            for table in ("pages", "observations", "quality_issues", "attempts"):
                fields = SCHEMA[table]
                snapshot[table] = _rows(
                    connection.execute(
                        f"SELECT {fields} FROM {table} WHERE job_id=? ORDER BY ALL", [job_id]
                    )
                )
            snapshot["versions"] = _rows(
                connection.execute(
                    "SELECT "
                    + ",".join("v." + field for field in SCHEMA["price_versions"].split(","))
                    + " FROM price_versions v WHERE version_id IN "
                    "(SELECT version_id FROM observations WHERE job_id=?) ORDER BY version_id",
                    [job_id],
                )
            )
        connection.execute("COMMIT")
        return snapshot


def _report(snapshot, params, now, max_age_hours):
    alerts = []

    def alert(code):
        if code not in alerts:
            alerts.append(code)

    result = {
        "schema_version": 1,
        "status": "attention",
        "mode": "saved_price_acquisition",
        "read_only": True,
        "real_execution_enabled": False,
        "live_connection_verified": False,
        "price_coverage_verified": False,
        "raw_files_verified": False,
        "operational_readiness": False,
        "observed_at": now.isoformat(),
        "query": params,
        "policy": {
            "selection": "latest_exact_query_started_at_then_job_id_including_future",
            "state_semantics": "current_stored_snapshot_not_historical_pit",
            "freshness": "terminal_updated_at_operational_threshold_not_provider_sla",
            "max_age_hours": max_age_hours,
            "provider_publication_inferred": False,
            "no_older_success_fallback": True,
        },
        "latest_job": None,
        "freshness": {"status": "unavailable", "age_hours": None},
        "quality": [],
        "http_attempts": {"count": 0, "outcomes": [], "statuses": []},
        "data": None,
        "historical_jobs": {"count": len(snapshot["matching_jobs"]), "by_state": {}},
    }
    historical = Counter()
    for job in snapshot["matching_jobs"]:
        _require(isinstance(job["job_id"], str) and re.fullmatch(r"[a-f0-9]{32}", job["job_id"]))
        _require(job["state"] in STATES)
        aware_utc(job["started_at"])
        aware_utc(job["updated_at"])
        historical[job["state"]] += 1
    result["historical_jobs"]["by_state"] = dict(sorted(historical.items()))
    chosen = snapshot["chosen"]
    if chosen is None:
        alert("exact_query_job_missing")
    else:
        state = chosen["state"]
        started, updated = aware_utc(chosen["started_at"]), aware_utc(chosen["updated_at"])
        _require(_integer(chosen["pages"]) and _integer(chosen["rows_received"]))
        _require(chosen["cursor"] is None or isinstance(chosen["cursor"], str))
        _require(chosen["plan"] in {"free", "light", "standard", "premium"})
        if state != "complete":
            alert("latest_job_" + state)
        if chosen["cursor"] is not None:
            alert("pagination_incomplete")
        if started > now or updated > now:
            alert("job_timestamp_future")
        if updated < started:
            alert("job_timestamp_order_invalid")
        result["latest_job"] = {
            "job_id": chosen["job_id"],
            "state": state,
            "plan": chosen["plan"],
            "started_at": started.isoformat(),
            "updated_at": updated.isoformat(),
            "finalized_at": updated.isoformat() if state in TERMINAL else None,
            "pages": chosen["pages"],
            "rows_received": chosen["rows_received"],
            "cursor_present": chosen["cursor"] is not None,
            "error_category": chosen["error_category"]
            if chosen["error_category"] in CATEGORIES
            else "other"
            if chosen["error_category"] is not None
            else None,
        }
        if state in TERMINAL:
            age_hours = (now - updated).total_seconds() / 3600
            freshness = (
                "future"
                if age_hours < 0
                else "stale"
                if age_hours > max_age_hours
                else "within_declared_age"
            )
            result["freshness"] = {"status": freshness, "age_hours": age_hours}
            if freshness != "within_declared_age":
                alert("completion_evidence_" + freshness)
        else:
            result["freshness"]["status"] = "not_finalized"
        pages, observations = snapshot["pages"], snapshot["observations"]
        issues, attempts = snapshot["quality_issues"], snapshot["attempts"]
        versions = {v["version_id"]: v for v in snapshot["versions"]}
        page_map = {}
        for page in pages:
            _require(_integer(page["page_number"]) and _integer(page["row_count"]))
            _require(page["page_number"] not in page_map)
            for field in ("request_cursor", "next_cursor"):
                _require(page[field] is None or isinstance(page[field], str))
            _require(
                isinstance(page["raw_sha256"], str)
                and re.fullmatch(r"[a-f0-9]{64}", page["raw_sha256"])
            )
            observed = aware_utc(page["observed_at"])
            if observed > now:
                alert("page_timestamp_future")
            if state in TERMINAL and observed > updated:
                alert("completion_precedes_page_observation")
            page_map[page["page_number"]] = page
        integrity = len(pages) == chosen["pages"] and all(
            number == index for index, number in enumerate(sorted(page_map))
        )
        cursor = None
        seen_cursors = set()
        for page in pages:
            integrity &= page["request_cursor"] == cursor
            if page["request_cursor"] is not None:
                integrity &= page["request_cursor"] not in seen_cursors
                seen_cursors.add(page["request_cursor"])
            if page["next_cursor"] is not None:
                integrity &= bool(page["next_cursor"]) and page["next_cursor"] not in seen_cursors
            cursor = page["next_cursor"]
        integrity &= cursor == chosen["cursor"]
        if state in TERMINAL | {"fetched"}:
            integrity &= bool(pages) and cursor is None
        if state in TERMINAL:
            integrity &= chosen["error_category"] is None
        locations, dates, keys, unlinked, mismatched, no_trade_locations = (
            set(),
            [],
            set(),
            0,
            0,
            set(),
        )
        for observation in observations:
            page, index = observation["page_number"], observation["row_index"]
            _require(_integer(page) and _integer(index))
            location = (page, index)
            if (
                location in locations
                or page not in page_map
                or index >= page_map[page]["row_count"]
            ):
                integrity = False
            locations.add(location)
            version_id = observation["version_id"]
            _require(version_id is None or _integer(version_id, 1))
            version = versions.get(version_id)
            if version is None:
                unlinked += 1
            else:
                _require(version["quality_status"] in {"ok", "error"})
                if version["quality_status"] == "error":
                    alert("stored_version_quality_error")
                if (
                    version["code"] != observation["code"]
                    or version["target_date"] != observation["target_date"]
                ):
                    mismatched += 1
                if aware_utc(version["observed_at"]) > now:
                    alert("version_observation_future")
                if page in page_map and version["observed_at"] > page_map[page]["observed_at"]:
                    integrity = False
                _require(
                    isinstance(version["content_hash"], str)
                    and re.fullmatch(r"[a-f0-9]{64}", version["content_hash"])
                )
            target = observation["target_date"]
            if target is None or observation["code"] != params["code"]:
                alert("observation_key_invalid_for_query")
                if state == "complete":
                    integrity = False
            if target is not None:
                _require(type(target) is date)
                dates.append(target)
                keys.add((observation["code"], target))
                if not params["from"] <= target.isoformat() <= params["to"]:
                    alert("observation_outside_requested_dates")
            if observation["code"] != params["code"]:
                alert("observation_code_mismatch")
        integrity &= (
            len(observations) == chosen["rows_received"] == sum(p["row_count"] for p in pages)
        )
        for page in pages:
            indexes = sorted(index for number, index in locations if number == page["page_number"])
            integrity &= len(indexes) == page["row_count"] and all(
                index == expected for expected, index in enumerate(indexes)
            )
        quality = Counter()
        for issue in issues:
            _require(issue["severity"] in {"info", "warning", "error"})
            _require(
                isinstance(issue["rule"], str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", issue["rule"])
            )
            location = (issue["page_number"], issue["row_index"])
            if location not in locations:
                integrity = False
            quality[(issue["severity"], issue["rule"])] += 1
            if issue["rule"] == "no_trades" and issue["severity"] == "info":
                no_trade_locations.add(location)
        result["quality"] = [
            {"severity": severity, "rule": rule, "count": count}
            for (severity, rule), count in sorted(quality.items())
        ]
        errors = sum(count for (severity, _), count in quality.items() if severity == "error")
        if errors:
            alert("latest_job_quality_errors")
        if state == "complete":
            integrity &= bool(observations) and not errors and not unlinked and not mismatched
        if state == "empty":
            integrity &= not observations and not errors
        if state == "quality_failed":
            integrity &= bool(errors)
        if not integrity or mismatched:
            alert("latest_job_metadata_integrity_mismatch")
        if unlinked:
            alert("unlinked_or_dangling_observations")
        result["data"] = {
            "observation_count": len(observations),
            "distinct_code_date_count": len(keys),
            "duplicate_code_date_observations": len(dates) - len(keys),
            "target_date_min": min(dates).isoformat() if dates else None,
            "target_date_max": max(dates).isoformat() if dates else None,
            "no_trade_observation_count": len(no_trade_locations),
            "unlinked_or_dangling_observation_count": unlinked,
            "observation_version_key_mismatches": mismatched,
        }
        outcome_counts, status_counts = Counter(), Counter()
        for attempt in attempts:
            attempted_at = aware_utc(attempt["attempted_at"])
            if attempted_at > now:
                alert("http_attempt_timestamp_future")
            _require(
                attempt["status"] is None
                or _integer(attempt["status"], 100)
                and attempt["status"] <= 599
            )
            category = attempt["outcome"] if attempt["outcome"] in CATEGORIES else "other"
            outcome_counts[category] += 1
            status_counts[
                str(attempt["status"]) if attempt["status"] is not None else "no_response"
            ] += 1
        result["http_attempts"] = {
            "count": len(attempts),
            "outcomes": [
                {"outcome": key, "count": value} for key, value in sorted(outcome_counts.items())
            ],
            "statuses": [
                {"status": key, "count": value} for key, value in sorted(status_counts.items())
            ],
        }
    result["alerts"] = [{"code": code} for code in alerts]
    result["status"] = "attention" if alerts else "observed"
    result["source"] = {"kind": "duckdb_read_transaction", "snapshot_hash": _hash(snapshot)}
    result["result_hash"] = _hash(result)
    return result


def monitor_prices(
    database: Path, *, code: str, start: str, end: str, now: datetime, max_age_hours: float
) -> dict:
    """Observe stored latest attempts; no API, file verification or historical replay."""
    try:
        _require(
            type(max_age_hours) in {int, float}
            and math.isfinite(max_age_hours)
            and max_age_hours > 0
        )
        params = price_query(code, start, end)
        now = aware_utc(now)
        snapshot = _snapshot(Path(database), params)
        return _report(snapshot, params, now, float(max_age_hours))
    except Exception:
        raise ValueError("Price monitoring source unavailable or invalid") from None
