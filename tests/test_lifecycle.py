"""Finalization and recovery invariants shared by Stage 1 dataset stores."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from trading_system import market_financial, reference
from trading_system.data_sources.jquants import JQuantsClient

OBSERVED = datetime(2026, 10, 23, tzinfo=timezone.utc)
PROFILES = {
    "master": (
        reference,
        {"date": "2026-10-22"},
        {
            "Date": "2026-10-22",
            "Code": "86970",
            "CoName": "Test",
            "Mkt": "0111",
            "S17": "10",
            "S33": "5250",
            "ProdCat": "011",
        },
    ),
    "calendar": (
        reference,
        {"from": "2026-10-22", "to": "2026-10-22"},
        {"Date": "2026-10-22", "HolDiv": "1"},
    ),
    "topix": (
        market_financial,
        {"from": "2026-10-22", "to": "2026-10-22"},
        {"Date": "2026-10-22", "O": 100, "H": 102, "L": 99, "C": 101},
    ),
    "financials": (
        market_financial,
        {"code": "86970"},
        {
            "Code": "86970",
            "DiscDate": "2026-10-22",
            "DiscTime": "15:00:00",
            "DiscNo": "20261022000001",
            "DocType": "ForecastRevision",
            "Sales": "100",
        },
    ),
}


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("lc")


@pytest.fixture(params=PROFILES)
def profile(request):
    dataset = request.param
    module, query, row = PROFILES[dataset]
    store_type = (
        reference.ReferenceStore if module is reference else market_financial.MarketFinancialStore
    )
    ingest = (
        reference.ingest_reference
        if module is reference
        else market_financial.ingest_market_financial
    )
    return dataset, module, query, row, store_type, ingest


def save(store, job_id, rows, cursor=None, observed=OBSERVED):
    body = {"data": rows, "pagination_key": cursor}
    store.save_page(job_id, body, json.dumps(body).encode(), observed)


def counts(store):
    return {
        table: store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("pages", "observations", "quality_issues")
    }


@pytest.mark.parametrize(
    "stage", ["new", "partial", "forged_empty", "forged_partial", "wrong_index"]
)
def test_finish_rejects_unproven_final_page_without_quality_or_job_mutation(
    tmp_path, profile, stage
):
    dataset, _, query, row, store_type, _ = profile
    with store_type(tmp_path, dataset) as store:
        job_id = store.start(query, "standard")["job_id"]
        if stage in {"partial", "forged_partial", "wrong_index"}:
            save(store, job_id, [row], cursor=None if stage == "wrong_index" else "next")
        if stage.startswith("forged"):
            store.fail(job_id, "export_failed")
            store.db.execute("UPDATE jobs SET cursor=NULL WHERE job_id=?", [job_id])
        if stage == "wrong_index":
            store.db.execute("UPDATE jobs SET pages=pages+1 WHERE job_id=?", [job_id])
        before, before_counts = store.job(job_id), counts(store)
        with pytest.raises(ValueError, match="final page"):
            store.finish(job_id)
        assert store.job(job_id) == before
        assert counts(store) == before_counts
        assert store.as_of(OBSERVED + timedelta(days=1000), query).num_rows == 0
        assert not (store.root / "normalized/latest.json").exists()


@pytest.mark.parametrize("state", ["complete", "empty", "quality_failed"])
def test_terminal_job_evidence_is_immutable_and_parquet_matches(tmp_path, profile, state):
    dataset, _, query, row, store_type, _ = profile
    rows = [] if state == "empty" else [row]
    if state == "quality_failed":
        rows = [row | {"DiscDate" if dataset == "financials" else "Date": "bad-date"}]
    with store_type(tmp_path, dataset) as store:
        job_id = store.start(query, "standard")["job_id"]
        save(store, job_id, rows)
        receipt = store.finish(job_id)
        assert receipt["state"] == state
        before, before_counts = store.job(job_id), counts(store)
        before_files = sorted(path.relative_to(store.root) for path in store.root.rglob("*"))
        store.fail(job_id, "export_failed")
        with pytest.raises(ValueError, match="final page"):
            store.finish(job_id)
        with pytest.raises(ValueError):
            save(store, job_id, [row])
        assert store.job(job_id) == before
        assert counts(store) == before_counts
        assert (
            sorted(path.relative_to(store.root) for path in store.root.rglob("*")) == before_files
        )
        manifest = json.loads((store.root / "normalized/latest.json").read_bytes())
        parquet = store.root / manifest["files"]["jobs"]["path"]
        assert store.db.execute(
            "SELECT state,updated_at,completed_at FROM read_parquet(?) WHERE job_id=?",
            [str(parquet), job_id],
        ).fetchone() == (state, before["updated_at"], before["completed_at"])


@pytest.mark.parametrize("export_failed", [False, True])
def test_fetched_and_export_retry_jobs_do_not_accept_additional_pages(
    tmp_path, profile, export_failed
):
    dataset, _, query, row, store_type, _ = profile
    with store_type(tmp_path, dataset) as store:
        job_id = store.start(query, "standard")["job_id"]
        save(store, job_id, [row])
        if export_failed:
            store.fail(job_id, "export_failed")
        before, before_counts = store.job(job_id), counts(store)
        raw_count = len(list(store.root.rglob("*.meta.json")))
        with pytest.raises(ValueError):
            save(store, job_id, [row])
        assert store.job(job_id) == before and counts(store) == before_counts
        assert len(list(store.root.rglob("*.meta.json"))) == raw_count
        assert store.finish(job_id)["state"] == "complete"


def test_failed_page_fetch_can_resume_and_completion_follows_latest_observation(
    tmp_path, profile, monkeypatch
):
    dataset, module, query, row, store_type, _ = profile
    with store_type(tmp_path, dataset) as store:
        job_id = store.start(query, "standard")["job_id"]
        latest = OBSERVED + timedelta(hours=1)
        save(store, job_id, [row], cursor="next", observed=latest)
        store.fail(job_id, "ingestion_failed")
        # A clock rollback cannot move completion before an earlier page's observation.
        save(store, job_id, [], observed=OBSERVED)
        monkeypatch.setattr(module, "utcnow", lambda: OBSERVED)
        assert store.finish(job_id)["state"] == "complete"
        job = store.job(job_id)
        assert job["updated_at"] == job["completed_at"] == latest
        assert store.as_of(latest, query).num_rows == 0
        assert store.as_of(latest + timedelta(seconds=1), query).num_rows == 1


def test_export_failure_retry_uses_final_page_without_http(tmp_path, profile, monkeypatch):
    dataset, _, query, row, store_type, ingest = profile
    original = store_type.export

    def fail_export(_):
        raise OSError("simulated export failure")

    monkeypatch.setattr(store_type, "export", fail_export)
    with (
        JQuantsClient(
            "test-secret",
            endpoint=dataset,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [row]})),
        ) as client,
        pytest.raises(OSError, match="export failure"),
    ):
        ingest(tmp_path, client, dataset, query, "standard")
    monkeypatch.setattr(store_type, "export", original)
    with store_type(tmp_path, dataset) as store:
        job_id = store.db.execute("SELECT job_id FROM jobs").fetchone()[0]
        assert store.job(job_id)["state"] == "failed"
        assert store.job(job_id)["error_category"] == "export_failed"
        assert counts(store)["pages"] == 1
    with JQuantsClient(
        "test-secret",
        endpoint=dataset,
        transport=httpx.MockTransport(lambda _: pytest.fail("Export retry accessed HTTP")),
    ) as client:
        assert ingest(tmp_path, client, dataset, query, "standard")["state"] == "complete"


def test_receipt_write_failure_preserves_commit_and_cached_retry(tmp_path, profile, monkeypatch):
    dataset, module, query, row, store_type, ingest = profile
    original = module.atomic_write

    def fail_receipt(path, content):
        if path.parent.name == "logs":
            raise OSError("simulated receipt failure")
        return original(path, content)

    monkeypatch.setattr(module, "atomic_write", fail_receipt)
    with (
        JQuantsClient(
            "test-secret",
            endpoint=dataset,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [row]})),
        ) as client,
        pytest.raises(OSError, match="receipt failure"),
    ):
        ingest(tmp_path, client, dataset, query, "standard")
    with store_type(tmp_path, dataset) as store:
        job_id = store.db.execute("SELECT job_id FROM jobs").fetchone()[0]
        before = store.job(job_id)
        assert before["state"] == "complete" and before["error_category"] is None
        assert store.as_of(before["completed_at"] + timedelta(seconds=1), query).num_rows == 1
    with JQuantsClient(
        "test-secret",
        endpoint=dataset,
        transport=httpx.MockTransport(lambda _: pytest.fail("Committed job accessed HTTP")),
    ) as client:
        cached = ingest(tmp_path, client, dataset, query, "standard")
        assert cached["cached"] and cached["verified_duckdb_read"]
    with store_type(tmp_path, dataset) as store:
        assert store.job(job_id) == before
