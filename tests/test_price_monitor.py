"""Latest-attempt stored price monitoring without APIs, migration or fallback."""

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import duckdb
import pytest

from trading_system.ingestion.prices import price_query
from trading_system.monitoring import prices
from trading_system.monitoring.prices import monitor_prices
from trading_system.storage.store import Store, canonical

NOW = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)
PARAMS = price_query("186a", "2026-10-01", "2026-10-20")


def bar(**changes):
    return {
        "Date": "2026-10-01",
        "Code": "186A0",
        "O": 100,
        "H": 110,
        "L": 90,
        "C": 105,
        "Vo": 1000,
        "Va": 100000,
        "AdjFactor": 1,
        "AdjO": 100,
        "AdjH": 110,
        "AdjL": 90,
        "AdjC": 105,
        "AdjVo": 1000,
    } | changes


def build(root, rows=None, *, when=NOW, params=PARAMS, state=None):
    with Store(root) as store, patch("trading_system.storage.store.utcnow", return_value=when):
        job = store.start(params, "standard", refresh=True)
        if rows is not None:
            body = {"data": rows}
            store.save_page(job["job_id"], body, canonical(body).encode(), when)
            if state is None:
                store.finish(job["job_id"])
        if state is not None:
            store.db.execute("UPDATE jobs SET state=? WHERE job_id=?", [state, job["job_id"]])
        store.attempt(job["job_id"], status=200, outcome="success")
        return root / "catalog.duckdb", job["job_id"]


@pytest.fixture
def source(tmp_path):
    return build(tmp_path / "prices", [bar()])


def observe(database, *, now=NOW, max_age_hours=24, **query):
    return monitor_prices(
        database,
        code=query.get("code", "186a"),
        start=query.get("start", PARAMS["from"]),
        end=query.get("end", PARAMS["to"]),
        now=now,
        max_age_hours=max_age_hours,
    )


def alert_codes(result):
    return {alert["code"] for alert in result["alerts"]}


def mutate(database, sql, args=None):
    with duckdb.connect(str(database)) as connection:
        connection.execute(sql, args or [])


def test_fresh_complete_normalized_query_deterministic_and_read_only(source, monkeypatch):
    database, job_id = source
    before = database.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("No initializer or network allowed")

    monkeypatch.setattr(Store, "__init__", forbidden)
    monkeypatch.setattr("trading_system.data_sources.jquants.JQuantsClient.__init__", forbidden)
    first = observe(database)
    assert first == observe(database)
    assert database.read_bytes() == before
    assert first["status"] == "observed"
    assert first["query"] == PARAMS
    assert first["latest_job"]["job_id"] == job_id
    assert first["freshness"] == {"status": "within_declared_age", "age_hours": 0}
    assert first["data"]["observation_count"] == 1
    assert first["data"]["target_date_min"] == first["data"]["target_date_max"] == "2026-10-01"
    for key in (
        "real_execution_enabled",
        "live_connection_verified",
        "price_coverage_verified",
        "raw_files_verified",
        "operational_readiness",
    ):
        assert first[key] is False
    assert str(database) not in canonical(first)
    assert "raw_path" not in canonical(first)
    assert (
        first["result_hash"]
        == hashlib.sha256(
            canonical({k: v for k, v in first.items() if k != "result_hash"}).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "hours,status", [(24, "within_declared_age"), (24.0001, "stale"), (-1, "future")]
)
def test_completion_freshness_boundary(source, hours, status):
    result = observe(source[0], now=NOW + timedelta(hours=hours))
    assert result["freshness"]["status"] == status
    assert result["status"] == ("observed" if status == "within_declared_age" else "attention")


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "24"])
def test_invalid_operational_age_fails_closed(source, value):
    with pytest.raises(ValueError, match="^Price monitoring source unavailable or invalid$"):
        observe(source[0], max_age_hours=value)


def test_latest_failed_attempt_masks_old_complete_and_future_is_not_filtered(source):
    database, old = source
    _, new = build(database.parent, when=NOW + timedelta(hours=1), state="failed")
    mutate(database, "UPDATE jobs SET error_category=? WHERE job_id=?", ["private_error_body", new])
    result = observe(database)
    assert result["latest_job"]["job_id"] == new != old
    assert result["latest_job"]["error_category"] == "other"
    assert {
        "latest_job_failed",
        "job_timestamp_future",
        "http_attempt_timestamp_future",
    } <= alert_codes(result)
    assert "private_error_body" not in canonical(result)
    assert result["freshness"]["status"] == "not_finalized"


def test_old_failed_attempt_is_history_not_current_alert(tmp_path):
    database, _ = build(tmp_path, when=NOW - timedelta(hours=1), state="failed")
    _, latest = build(tmp_path, [bar()])
    result = observe(database)
    assert result["status"] == "observed"
    assert result["historical_jobs"]["by_state"] == {"complete": 1, "failed": 1}
    assert result["latest_job"]["job_id"] == latest


def test_exact_query_isolation_even_newer_other_query_failed(source):
    database, selected = source
    other = price_query("8697", PARAMS["from"], PARAMS["to"])
    build(database.parent, params=other, when=NOW + timedelta(hours=1), state="failed")
    result = observe(database)
    assert result["status"] == "observed"
    assert result["latest_job"]["job_id"] == selected
    assert result["historical_jobs"]["count"] == 1
    missing = observe(database, start="2026-10-02")
    assert missing["latest_job"] is None
    assert alert_codes(missing) == {"exact_query_job_missing"}


@pytest.mark.parametrize("state", ["running", "fetched", "empty", "quality_failed"])
def test_non_complete_latest_state_is_attention(tmp_path, state):
    rows = (
        []
        if state == "empty"
        else [bar(C=None)]
        if state == "quality_failed"
        else [bar()]
        if state == "fetched"
        else None
    )
    database, _ = build(tmp_path, rows, state=state if state in {"running", "fetched"} else None)
    result = observe(database)
    assert result["latest_job"]["state"] == state
    assert "latest_job_" + state in alert_codes(result)


def test_quality_failed_null_keys_preserved_not_unreadable(tmp_path):
    database, _ = build(tmp_path, [bar(Date="invalid", Code=None)])
    result = observe(database)
    assert result["latest_job"]["state"] == "quality_failed"
    assert result["data"]["observation_count"] == 1
    assert result["data"]["unlinked_or_dangling_observation_count"] == 1
    assert "latest_job_quality_errors" in alert_codes(result)


def test_no_trade_semantics_and_identical_reuse_are_not_missing_data(tmp_path):
    row = bar(
        **{
            name: None
            for name in ("O", "H", "L", "C", "Vo", "Va", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo")
        }
    )
    database, _ = build(tmp_path, [row])
    _, latest = build(tmp_path, [row], when=NOW + timedelta(hours=1))
    result = observe(database, now=NOW + timedelta(hours=1))
    assert result["status"] == "observed"
    assert result["latest_job"]["job_id"] == latest
    assert result["data"]["no_trade_observation_count"] == 1
    assert result["quality"] == [{"severity": "info", "rule": "no_trades", "count": 1}]


def test_paginated_duplicate_identical_rows_keep_lineage_and_quality_warning(tmp_path):
    with Store(tmp_path) as store, patch("trading_system.storage.store.utcnow", return_value=NOW):
        job = store.start(PARAMS, "standard")
        first = {"data": [bar()], "pagination_key": "page-two"}
        last = {"data": [bar()]}
        store.save_page(job["job_id"], first, canonical(first).encode(), NOW)
        store.save_page(job["job_id"], last, canonical(last).encode(), NOW)
        store.finish(job["job_id"])
    database = tmp_path / "catalog.duckdb"
    result = observe(database)
    assert result["status"] == "observed"
    assert result["data"]["observation_count"] == 2
    assert result["data"]["distinct_code_date_count"] == 1
    assert result["data"]["duplicate_code_date_observations"] == 1
    assert result["quality"] == [{"severity": "warning", "rule": "duplicate_identical", "count": 1}]
    mutate(database, "UPDATE pages SET request_cursor='page-two' WHERE page_number=0")
    assert "latest_job_metadata_integrity_mismatch" in alert_codes(observe(database))


def test_deterministic_tie_break_uses_job_id_without_current_clock_filter(source):
    database, old = source
    _, second = build(database.parent, [bar()])
    result = observe(database)
    assert result["latest_job"]["job_id"] == max(old, second)


def test_view_substitution_is_rejected_before_evaluation(source):
    database, _ = source
    mutate(database, "ALTER TABLE pages RENAME TO saved_pages")
    mutate(database, "CREATE VIEW pages AS SELECT * FROM saved_pages")
    with pytest.raises(ValueError, match="^Price monitoring source unavailable or invalid$"):
        observe(database)


@pytest.mark.parametrize(
    "damage",
    [
        "missing_page",
        "missing_observation",
        "dangling",
        "row_count",
        "wrong_key",
        "null_date",
        "late_version",
        "terminal_error",
        "cursor",
    ],
)
def test_complete_metadata_corruption_never_observed(source, damage):
    database, job_id = source
    statements = {
        "missing_page": "DELETE FROM pages",
        "missing_observation": "DELETE FROM observations",
        "dangling": "DELETE FROM price_versions",
        "row_count": "UPDATE pages SET row_count=2",
        "wrong_key": "UPDATE observations SET code='86970'",
        "null_date": "UPDATE observations SET target_date=NULL",
        "late_version": "UPDATE price_versions SET observed_at=observed_at+INTERVAL '1 second'",
        "terminal_error": "UPDATE jobs SET error_category='export_failed'",
        "cursor": "UPDATE jobs SET cursor='incomplete'",
    }
    mutate(database, statements[damage])
    result = observe(database)
    assert result["status"] == "attention"
    assert "latest_job_metadata_integrity_mismatch" in alert_codes(result)
    assert result["latest_job"]["job_id"] == job_id


def test_partial_cursor_is_preserved_and_retry_attempts_are_bounded(source):
    database, job_id = source
    mutate(
        database,
        "UPDATE jobs SET state='failed',cursor='private_cursor',error_category='transport_error'",
    )
    with duckdb.connect(str(database)) as connection:
        connection.executemany(
            "INSERT INTO attempts VALUES(?,?,?,?)",
            [
                [job_id, NOW, 429, "rate_limited"],
                [job_id, NOW, None, "transport_error"],
                [job_id, NOW, 500, "private_url_or_error_body"],
            ],
        )
    result = observe(database)
    assert "pagination_incomplete" in alert_codes(result)
    assert result["http_attempts"]["count"] == 4
    assert {item["outcome"] for item in result["http_attempts"]["outcomes"]} == {
        "success",
        "rate_limited",
        "transport_error",
        "other",
    }
    assert "private_" not in canonical(result)


@pytest.mark.parametrize(
    "damage", ["missing_table", "unknown_state", "bad_params", "duplicate_params", "missing_time"]
)
def test_malformed_source_raises_sanitized_error(source, damage):
    database, _ = source
    statements = {
        "missing_table": "DROP TABLE pages",
        "unknown_state": "UPDATE jobs SET state='private_unknown_state'",
        "bad_params": "UPDATE jobs SET params_json='private_invalid_json'",
        "duplicate_params": "UPDATE jobs SET params_json='{"
        + '"code":"186A0","code":"86970","from":"2026-10-01","to":"2026-10-20"'
        + "}'",
        "missing_time": "UPDATE jobs SET updated_at=NULL",
    }
    mutate(database, statements[damage])
    with pytest.raises(ValueError) as caught:
        observe(database)
    assert str(caught.value) == "Price monitoring source unavailable or invalid"
    assert caught.value.__cause__ is None


def test_missing_database_never_created_and_naive_clock_rejected(tmp_path, source):
    missing = tmp_path / "missing" / "catalog.duckdb"
    with pytest.raises(ValueError):
        observe(missing)
    assert not missing.parent.exists()
    with pytest.raises(ValueError):
        observe(source[0], now=NOW.replace(tzinfo=None))


def test_read_only_connection_explicit_transaction_and_no_schema_writes(source, monkeypatch):
    original_connect = duckdb.connect
    statements = []

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.connection.close()

        def execute(self, statement, *args):
            statements.append(statement)
            return self.connection.execute(statement, *args)

    def connect(database, **kwargs):
        assert kwargs == {"read_only": True}
        return Connection(original_connect(database, **kwargs))

    monkeypatch.setattr(prices.duckdb, "connect", connect)
    assert observe(source[0])["status"] == "observed"
    assert statements[0] == "BEGIN TRANSACTION" and statements[-1] == "COMMIT"
    assert all(statement.startswith(("SELECT", "BEGIN", "COMMIT")) for statement in statements)
