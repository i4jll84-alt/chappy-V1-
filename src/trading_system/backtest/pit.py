"""Read-only availability proof; source prices alone cannot authorize a real replay."""

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from trading_system.backtest.engine import encode
from trading_system.ingestion.prices import price_query
from trading_system.point_in_time.policy import aware_utc
from trading_system.storage.store import canonical, digest


def load_price_snapshot(catalog: Path, params: dict, decision_at: datetime, required_dates) -> dict:
    catalog = Path(catalog)
    if not catalog.is_file():
        raise ValueError("price_catalog_missing")
    if set(params) != {"code", "from", "to"}:
        raise ValueError("exact_price_query_required")
    params = price_query(params["code"], params["from"], params["to"])
    decision_at = aware_utc(decision_at)
    required = {
        date.fromisoformat(value) if isinstance(value, str) else value for value in required_dates
    }
    if not required or any(not isinstance(day, date) for day in required):
        raise ValueError("required_source_dates_missing")
    if any(not params["from"] <= day.isoformat() <= params["to"] for day in required):
        raise ValueError("required_source_date_outside_query")
    with duckdb.connect(str(catalog), read_only=True) as db:
        db.execute("SET TimeZone='UTC'")
        chosen = db.execute(
            "SELECT job_id,state,updated_at,rows_received FROM jobs WHERE params_json=? "
            "AND state IN ('complete','empty','quality_failed') AND updated_at<=? "
            "ORDER BY updated_at DESC,started_at DESC,job_id DESC LIMIT 1",
            [canonical(params), decision_at - timedelta(seconds=1)],
        ).fetchone()
        if chosen is None:
            raise ValueError("no_completed_query_available_at_decision")
        job_id, state, completed, received = chosen
        if state != "complete":
            raise ValueError("latest_finalized_query_not_clean")
        observed = db.execute(
            "SELECT count(*),count(v.version_id) FROM observations o LEFT JOIN price_versions v "
            "ON o.version_id=v.version_id WHERE o.job_id=?",
            [job_id],
        ).fetchone()
        if observed != (received, received):
            raise ValueError("incomplete_snapshot_lineage")
        rows = db.execute(
            "SELECT v.*,o.page_number,o.row_index FROM observations o JOIN price_versions v "
            "ON o.version_id=v.version_id WHERE o.job_id=? QUALIFY row_number() OVER ("
            "PARTITION BY o.code,o.target_date ORDER BY o.page_number DESC,o.row_index DESC)=1",
            [job_id],
        )
        columns = [column[0] for column in rows.description]
        records = [dict(zip(columns, row, strict=True)) for row in rows.fetchall()]
    selected = []
    for day in sorted(required):
        matches = [r for r in records if r["target_date"] == day and r["code"] == params["code"]]
        if len(matches) != 1:
            raise ValueError("required_price_missing_from_snapshot")
        row = matches[0]
        if row["quality_status"] != "ok" or row["available_at"] is None:
            raise ValueError("required_price_not_qualified")
        effective = max(row["available_at"], completed + timedelta(seconds=1))
        if effective > decision_at or row["observed_at"] > decision_at:
            raise ValueError("required_price_not_yet_available")
        selected.append(
            {
                "code": row["code"],
                "date": day,
                "version_id": row["version_id"],
                "content_hash": row["content_hash"],
                "close": row["close"],
                "effective_available_at": effective,
                "raw_path": row["raw_path"],
            }
        )
    evidence = encode(
        {
            "catalog": str(catalog.resolve()),
            "query": params,
            "decision_at": decision_at,
            "snapshot_job_id": job_id,
            "snapshot_finalized_at": completed,
            "finalization_source": "terminal_updated_at",
            "records": selected,
            "proves": "stored_price_availability_only",
            "real_execution_enabled": False,
        }
    )
    evidence["evidence_hash"] = digest(canonical(evidence).encode())
    return evidence


def validate_real_pit(catalog, params, checks) -> dict:
    """Audit each requested decision; real execution stays disabled even after success."""
    snapshots, errors = [], []
    if not checks:
        errors.append("decision_checks_required")
    for check in checks:
        try:
            snapshots.append(
                load_price_snapshot(catalog, params, check["decision_at"], check["required_dates"])
            )
        except ValueError as exc:
            errors.append(str(exc))
    return {
        "status": "blocked",
        "real_execution_enabled": False,
        "price_availability_verified": bool(checks) and not errors,
        "reasons": errors + ["historical_universe_rules_actions_short_inventory_not_verified"],
        "snapshots": snapshots,
    }
