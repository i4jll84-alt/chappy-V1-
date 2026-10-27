import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from trading_system.backtest.pit import load_price_snapshot, validate_real_pit
from trading_system.storage.store import Store

WHEN = datetime(2026, 10, 23, tzinfo=timezone.utc)
QUERY = {"code": "86970", "from": "2026-10-01", "to": "2026-10-22"}
REQUIRED = ["2026-10-22"]
ROW = {
    "Date": "2026-10-22",
    "Code": "86970",
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
}


def save(root, when=WHEN, rows=None, failed=False):
    with patch("trading_system.storage.store.utcnow", return_value=when), Store(root) as store:
        job = store.start(QUERY, "standard", refresh=True)["job_id"]
        if failed:
            store.fail(job, "network_failure")
        else:
            body = {"data": [ROW] if rows is None else rows}
            store.save_page(job, body, json.dumps(body).encode(), when)
            store.finish(job)
        return job


def test_availability_boundary_and_read_only_catalog_integrity(tmp_path):
    root = tmp_path / "p"
    job = save(root)
    catalog = root / "catalog.duckdb"
    before = hashlib.sha256(catalog.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="no_completed_query"):
        load_price_snapshot(catalog, QUERY, WHEN, REQUIRED)
    result = load_price_snapshot(catalog, QUERY, WHEN + timedelta(seconds=1), REQUIRED)
    assert result["snapshot_job_id"] == job
    assert result["records"][0]["version_id"] == 1
    assert result["real_execution_enabled"] is False
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("kind", ["empty", "quality_failed", "failed"])
def test_newer_query_state_never_leaks_old_clean_snapshot(tmp_path, kind):
    root = tmp_path / "p"
    original = save(root)
    later = WHEN + timedelta(seconds=10)
    save(root, later, [] if kind == "empty" else [ROW | {"C": -1}], kind == "failed")
    if kind == "failed":
        result = load_price_snapshot(
            root / "catalog.duckdb", QUERY, later + timedelta(seconds=1), REQUIRED
        )
        assert result["snapshot_job_id"] == original
    else:
        with pytest.raises(ValueError, match="latest_finalized_query_not_clean"):
            load_price_snapshot(
                root / "catalog.duckdb", QUERY, later + timedelta(seconds=1), REQUIRED
            )


def test_required_date_and_exact_query_no_fallback(tmp_path):
    root = tmp_path / "p"
    save(root)
    catalog = root / "catalog.duckdb"
    cutoff = WHEN + timedelta(seconds=1)
    with pytest.raises(ValueError, match="no_completed_query"):
        load_price_snapshot(catalog, QUERY | {"from": "2026-10-02"}, cutoff, REQUIRED)
    with pytest.raises(ValueError, match="required_price_missing"):
        load_price_snapshot(catalog, QUERY, cutoff, ["2026-10-21"])
    with pytest.raises(ValueError, match="outside_query"):
        load_price_snapshot(catalog, QUERY, cutoff, ["2026-10-23"])


def test_orphaned_observation_rejected(tmp_path):
    root = tmp_path / "p"
    save(root)
    with Store(root) as store:
        store.db.execute("UPDATE observations SET version_id=999")
    with pytest.raises(ValueError, match="incomplete_snapshot_lineage"):
        load_price_snapshot(root / "catalog.duckdb", QUERY, WHEN + timedelta(seconds=1), REQUIRED)


def test_missing_catalog_not_created_and_real_execution_stays_blocked(tmp_path):
    root = tmp_path / "p"
    catalog = root / "catalog.duckdb"
    checks = [{"decision_at": WHEN + timedelta(seconds=1), "required_dates": REQUIRED}]
    failed = validate_real_pit(catalog, QUERY, checks)
    assert failed["status"] == "blocked"
    assert "price_catalog_missing" in failed["reasons"]
    assert not root.exists()
    save(root)
    proved = validate_real_pit(catalog, QUERY, checks)
    assert proved["price_availability_verified"]
    assert proved["status"] == "blocked"
    assert not proved["real_execution_enabled"]
