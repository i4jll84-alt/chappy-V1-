import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from trading_system.data_sources.jquants import APIError, JQuantsClient
from trading_system.reference import ReferenceStore, ingest_reference, reference_query

MASTER = reference_query("master", date="2026-10-23")
CALENDAR = reference_query("calendar", start="2027-01-01", end="2027-01-02")
OBSERVED = datetime(2026, 10, 23, tzinfo=timezone.utc)


def master(**changes):
    return {
        "Date": "2026-10-23",
        "Code": "186A0",
        "CoName": "Test",
        "Mkt": "0111",
        "S17": "10",
        "S33": "5250",
        "ProdCat": "011",
    } | changes


def api(dataset, handler):
    return JQuantsClient(
        "test-secret",
        endpoint=dataset,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def ingest(root, rows, dataset="master", refresh=False):
    with api(dataset, lambda _: httpx.Response(200, json={"data": rows})) as client:
        return ingest_reference(
            root, client, dataset, MASTER if dataset == "master" else CALENDAR, "standard", refresh
        )


def save(store, rows, observed=OBSERVED, cursor=None, job_id=None):
    if job_id is None:
        job_id = store.start(
            MASTER if store.dataset == "master" else CALENDAR, "standard", refresh=True
        )["job_id"]
    body = {"data": rows, "pagination_key": cursor}
    store.save_page(job_id, body, json.dumps(body).encode(), observed)
    return job_id


@pytest.mark.parametrize(
    "dataset,kwargs",
    [
        ("master", {}),
        ("master", {"date": "20261023"}),
        ("master", {"date": "2026-10-23", "code": 1234}),
        ("master", {"date": "2026-10-23", "start": "2026-10-23"}),
        ("calendar", {"start": "2026-10-23"}),
        ("calendar", {"start": "2026-10-24", "end": "2026-10-23"}),
        ("calendar", {"start": "2026-10-23", "end": "2026-10-23", "code": "186A"}),
        ("other", {}),
    ],
)
def test_query_requires_explicit_dataset_bounds(dataset, kwargs):
    with pytest.raises(ValueError):
        reference_query(dataset, **kwargs)


def test_query_accepts_future_dates_and_normalizes_code():
    assert reference_query("master", date="2030-01-01", code="186a") == {
        "date": "2030-01-01",
        "code": "186A0",
    }
    assert reference_query("calendar", start="2030-01-01", end="2030-01-01")["to"] == "2030-01-01"


@pytest.mark.parametrize("dataset", ["master", "calendar"])
def test_nonempty_raw_parquet_and_repeat(tmp_path, dataset):
    rows = (
        [master(Unknown={"retained": True})]
        if dataset == "master"
        else [{"Date": "2027-01-01", "HolDiv": "3"}, {"Date": "2027-01-02", "HolDiv": "0"}]
    )
    result = ingest(tmp_path, rows, dataset)
    assert result["state"] == "complete"
    assert result["verified_duckdb_read"]
    with api(dataset, lambda _: pytest.fail("Cached request made HTTP call")) as client:
        cached = ingest_reference(
            tmp_path, client, dataset, MASTER if dataset == "master" else CALENDAR, "standard"
        )
    assert cached["cached"]
    assert cached["job_id"] == result["job_id"]
    assert ingest(tmp_path, rows, dataset, refresh=True)["versions"] == len(rows)
    with ReferenceStore(tmp_path, dataset) as store:
        manifest = json.loads((store.root / "normalized/latest.json").read_bytes())
        for entry in manifest["files"].values():
            path = store.root / entry["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
            assert (
                store.db.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
                == entry["rows"]
            )
        assert store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 2
        if dataset == "master":
            payload = store.db.execute("SELECT payload_json FROM reference_current").fetchone()[0]
            assert json.loads(payload)["Unknown"] == {"retained": True}
        else:
            assert store.db.execute(
                "SELECT is_tse_business_day FROM reference_current"
            ).fetchall() == [(False,), (False,)]


@pytest.mark.parametrize("field", ["Date", "Code", "CoName", "Mkt", "S17", "S33", "ProdCat"])
@pytest.mark.parametrize("value", [None, [], {}, 123])
def test_master_invalid_fields_are_retained_and_quarantined(tmp_path, field, value):
    result = ingest(tmp_path, [master(**{field: value})])
    assert result["state"] == "quality_failed"
    with ReferenceStore(tmp_path, "master") as store:
        # The random receipt suffix may end in 'a'; select the catalogued page.
        raw = (
            store.root
            / store.db.execute(
                "SELECT raw_path FROM pages ORDER BY page_number LIMIT 1"
            ).fetchone()[0]
        )
        assert json.loads(raw.read_bytes())["data"][0][field] == value
        assert store.as_of(datetime.now(timezone.utc) + timedelta(seconds=2)).num_rows == 0


@pytest.mark.parametrize("value", [None, [], {}, 1, "4"])
def test_calendar_invalid_division_is_retained(tmp_path, value):
    result = ingest(
        tmp_path,
        [{"Date": "2027-01-01", "HolDiv": value}, {"Date": "2027-01-02", "HolDiv": "1"}],
        "calendar",
    )
    assert result["state"] == "quality_failed"


def test_calendar_all_divisions_and_outside_range(tmp_path):
    with ReferenceStore(tmp_path, "calendar") as store:
        for i, value in enumerate(["0", "1", "2", "3"]):
            params = reference_query("calendar", start=f"2027-01-0{i + 1}", end=f"2027-01-0{i + 1}")
            job = store.start(params, "standard")
            body = {"data": [{"Date": f"2027-01-0{i + 1}", "HolDiv": value}]}
            store.save_page(job["job_id"], body, json.dumps(body).encode(), OBSERVED)
            assert store.finish(job["job_id"])["state"] == "complete"
        assert store.db.execute(
            "SELECT is_tse_business_day FROM reference_current ORDER BY target_date"
        ).fetchall() == [(False,), (True,), (True,), (False,)]
    result = ingest(tmp_path, [{"Date": "2028-01-01", "HolDiv": "1"}], "calendar")
    assert result["state"] == "quality_failed"


def test_calendar_missing_days_and_empty_are_distinct(tmp_path):
    assert ingest(tmp_path, [], "calendar")["state"] == "empty"
    assert (
        ingest(tmp_path, [{"Date": "2027-01-01", "HolDiv": "1"}], "calendar", refresh=True)["state"]
        == "quality_failed"
    )
    with ReferenceStore(tmp_path, "calendar") as store:
        assert (
            store.db.execute(
                "SELECT count(*) FROM quality_issues WHERE rule='missing_calendar_dates'"
            ).fetchone()[0]
            == 1
        )


def test_master_mapped_date_is_warning_but_mixed_dates_are_error(tmp_path, monkeypatch):
    # Both ingestion and direct saved pages must share a deterministic clock.
    monkeypatch.setattr("trading_system.reference.utcnow", lambda: OBSERVED)
    assert ingest(tmp_path, [master(Date="2026-10-26")])["state"] == "complete"
    with ReferenceStore(tmp_path, "master") as store:
        job_id = save(store, [master()], cursor="next")
        save(store, [master(Date="2026-10-26")], OBSERVED + timedelta(seconds=1), job_id=job_id)
        assert store.finish(job_id)["state"] == "quality_failed"
        assert (
            store.db.execute(
                "SELECT count(*) FROM quality_issues WHERE rule='snapshot_mixed_dates'"
            ).fetchone()[0]
            == 1
        )


def test_same_job_conflict_stays_quarantined_then_clean_scan_recovers(tmp_path):
    with ReferenceStore(tmp_path, "master") as store:
        job_id = save(store, [master(), master(CoName="B"), master(CoName="B")])
        assert store.finish(job_id)["state"] == "quality_failed"
        rows = store.db.execute(
            "SELECT quality_status FROM reference_versions ORDER BY version_id"
        ).fetchall()
        assert rows == [("ok",), ("error",)]
        save(store, [master(CoName="B")], OBSERVED + timedelta(hours=1))
        assert store.summary()["versions"] == 3
        assert (
            store.db.execute("SELECT quality_status FROM reference_current").fetchone()[0] == "ok"
        )


def test_corrections_a_b_a_and_unknown_publication(tmp_path):
    with ReferenceStore(tmp_path, "master") as store:
        for index, name in enumerate(["A", "B", "A"]):
            save(store, [master(CoName=name)], OBSERVED + timedelta(hours=index))
        rows = store.db.execute(
            "SELECT version_id,supersedes_version_id,company_name,"
            "published_at,source_updated_at,available_at-observed_at "
            "FROM reference_versions ORDER BY version_id"
        ).fetchall()
        assert [row[2] for row in rows] == ["A", "B", "A"]
        assert rows[1][1] == rows[0][0]
        assert rows[2][1] == rows[1][0]
        assert all(row[3:] == (None, None, timedelta(seconds=1)) for row in rows)


def test_snapshot_pit_waits_for_completion_and_removes_absent_members(tmp_path, monkeypatch):
    import trading_system.reference as module

    clock = OBSERVED
    monkeypatch.setattr(module, "utcnow", lambda: clock)
    with ReferenceStore(tmp_path, "master") as store:
        first = save(store, [master(), master(Code="12340")])
        assert store.as_of(clock + timedelta(days=1)).num_rows == 0
        store.finish(first)
        assert store.as_of(clock).num_rows == 0
        assert store.as_of(clock + timedelta(seconds=1)).num_rows == 2
        clock += timedelta(hours=1)
        second = save(store, [master()], observed=clock)
        assert store.as_of(clock + timedelta(seconds=1)).num_rows == 2
        store.finish(second)
        assert store.as_of(clock + timedelta(seconds=1))["code"].to_pylist() == ["186A0"]
        clock += timedelta(hours=1)
        third = save(store, [master(CoName=None)], observed=clock)
        store.finish(third)
        assert store.as_of(clock + timedelta(seconds=1), MASTER).num_rows == 0


def test_failure_resumes_next_page_without_refetch(tmp_path):
    calls = []

    def handler(request):
        cursor = request.url.params.get("pagination_key")
        calls.append(cursor)
        return (
            httpx.Response(401)
            if cursor
            else httpx.Response(200, json={"data": [master()], "pagination_key": "next"})
        )

    with api("master", handler) as client, pytest.raises(APIError):
        ingest_reference(tmp_path, client, "master", MASTER, "standard")
    with api(
        "master", lambda request: httpx.Response(200, json={"data": [master(Code="12340")]})
    ) as client:
        assert (
            ingest_reference(tmp_path, client, "master", MASTER, "standard")["rows_received"] == 2
        )
    assert calls == [None, "next"]


def test_page_and_checkpoint_rollback_together(tmp_path, monkeypatch):
    with ReferenceStore(tmp_path, "master") as store:
        job = store.start(MASTER, "standard")
        original = store._save_row

        def fail_after_row(*args):
            original(*args)
            raise RuntimeError("Injected transaction failure")

        monkeypatch.setattr(store, "_save_row", fail_after_row)
        with pytest.raises(RuntimeError):
            save(store, [master()], job_id=job["job_id"])
        assert store.job(job["job_id"])["pages"] == 0
        assert store.summary()["versions"] == 0
        assert store.db.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
        assert len(list(store.root.glob("raw/*/*.meta.json"))) == 1


def test_cursor_cycle_keeps_raw_and_checkpoint(tmp_path):
    with ReferenceStore(tmp_path, "master") as store:
        job_id = save(store, [master()], cursor="next")
        with pytest.raises(ValueError, match="cycle"):
            save(store, [master()], cursor="next", job_id=job_id)
        assert store.job(job_id)["pages"] == 1
        assert store.db.execute("SELECT count(*) FROM rejected_pages").fetchone()[0] == 1
        assert len(list(store.root.glob("raw/*/*.meta.json"))) == 2


def test_export_failure_retries_without_network(tmp_path, monkeypatch):
    original = ReferenceStore.export
    monkeypatch.setattr(ReferenceStore, "export", lambda _: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        ingest(tmp_path, [master()])
    monkeypatch.setattr(ReferenceStore, "export", original)
    with api("master", lambda _: pytest.fail("Refetched after export failure")) as client:
        assert (
            ingest_reference(tmp_path, client, "master", MASTER, "standard")["state"] == "complete"
        )


def test_cached_parquet_is_regenerated_if_deleted(tmp_path):
    first = ingest(tmp_path, [master()])
    root = tmp_path / "reference/master"
    (root / "normalized" / first["parquet_generation"] / "reference_current.parquet").unlink()
    with api("master", lambda _: pytest.fail("Reconstruction made HTTP request")) as client:
        second = ingest_reference(tmp_path, client, "master", MASTER, "standard")
    assert second["parquet_generation"] != first["parquet_generation"]
    assert second["versions"] == 1


def test_endpoint_mismatch_rejected_before_http(tmp_path):
    with api("calendar", lambda _: pytest.fail("Mismatched endpoint reached network")) as client:
        with pytest.raises(ValueError, match="endpoint"):
            ingest_reference(tmp_path, client, "master", MASTER, "standard")
