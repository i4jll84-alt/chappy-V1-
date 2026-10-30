import gzip
from datetime import date

import httpx
import pyarrow.parquet as pq
import pytest

from trading_system.history_download import (
    Archive,
    FetchError,
    HistoryClient,
    identity,
    validate_rows,
    validated_url,
)


@pytest.fixture
def archive(tmp_path):
    result = Archive(tmp_path)
    yield result
    result.close()


def client_for(root, handler, downloads=None):
    return HistoryClient(
        "test-secret",
        root,
        interval=0,
        transport=httpx.MockTransport(handler),
        download_transport=httpx.MockTransport(downloads or handler),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://bucket.r2.cloudflarestorage.com/x",
        "https://bucket.r2.cloudflarestorage.com.evil.example/x",
        "https://user:pass@bucket.r2.cloudflarestorage.com/x",
        "https://localhost/x",
        "https://127.0.0.1/x",
    ],
)
def test_reject_untrusted_storage(url):
    with pytest.raises(ValueError, match="invalid_download_url"):
        validated_url(url)


def test_credentials_stay_on_api_and_bulk_is_reproducible(archive):
    body = gzip.compress(
        b"Date,Code,O,H,L,C,Vo,Va\n2020-01-01,01230,100,102,99,101,100,10100\n2020-01-02,01230,,,,,,\n"
    )
    meta = {
        "Key": "equities/bars/daily/historical/2020/test.csv.gz",
        "LastModified": "2026-10-31T00:00:00+00:00",
        "Size": len(body),
    }

    def api(request):
        assert request.headers["x-api-key"] == "test-secret"
        assert request.url.host == "api.jquants.com"
        if request.url.path == "/v2/bulk/list":
            return httpx.Response(200, json={"data": [meta]})
        assert request.url.path == "/v2/bulk/get"
        return httpx.Response(
            200, json={"url": "https://bucket.r2.cloudflarestorage.com/file?token=private"}
        )

    def download(request):
        assert "x-api-key" not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(body))

    client = client_for(archive.root, api, download)
    try:
        archive.discover(client, "/equities/bars/daily")
        archive.discover(client, "/equities/bars/daily")
        assert archive.db.execute("select count(*) from bulk_files").fetchone()[0] == 1
        item = archive.db.execute("select * from bulk_files").fetchone()
        archive.fetch_bulk(client, item)
        item = archive.db.execute("select * from bulk_files").fetchone()
        assert item["state"] == "complete" and item["rows"] == 2
        rows = pq.read_table(archive.root / item["parquet_path"]).to_pylist()
        assert rows[0]["Code"] == "01230" and rows[1]["O"] == ""
        assert (archive.root / item["raw_path"]).read_bytes() == body
        assert item["min_date"] == "2020-01-01"
        from trading_system.history_audit import audit_history

        audit = audit_history(archive.root)
        assert audit["daily_prices"]["rows"] == 2
        assert audit["daily_prices"]["codes"] == 1
        assert audit["daily_prices"]["no_trade_rows"] == 1
        assert item["available_at"] > item["observed_at"]
        assert b"test-secret" not in (archive.root / "history.sqlite").read_bytes()
    finally:
        client.close()


def test_discovery_denial_is_recorded_without_fake_files(archive):
    client = client_for(archive.root, lambda _: httpx.Response(403, json={"message": "no"}))
    try:
        archive.discover(client, "/fins/details")
        assert archive.db.execute("select http_status from discoveries").fetchone()[0] == 403
        assert archive.db.execute("select count(*) from bulk_files").fetchone()[0] == 0
    finally:
        client.close()


def test_resume_json_cursor_atomic_records_and_no_duplicate_pages(archive):
    archive.seed_json(date(2026, 10, 30), date(2026, 10, 30))
    job = archive.db.execute(
        "select * from api_jobs where endpoint='/edinet/major-shareholders'"
    ).fetchone()

    def handler(request):
        if "pagination_key" in request.url.params:
            return httpx.Response(200, json={"data": [{"DocId": "B", "Code": "01230"}]})
        return httpx.Response(
            200, json={"data": [{"DocId": "A", "Code": "01230"}], "pagination_key": "next"}
        )

    client = client_for(archive.root, handler)
    try:
        archive.fetch_json_page(client, job)
        job = archive.db.execute("select * from api_jobs where id=?", (job["id"],)).fetchone()
        assert job["state"] == "running" and job["cursor"] == "next" and job["rows"] == 1
        archive.fetch_json_page(client, job)
        job = archive.db.execute("select * from api_jobs where id=?", (job["id"],)).fetchone()
        assert job["state"] == "complete" and job["rows"] == 2 and job["cursor"] is None
        assert archive.db.execute("select count(*) from api_records").fetchone()[0] == 2
        assert archive.db.execute("select count(*) from api_pages").fetchone()[0] == 2
    finally:
        client.close()


def test_cycle_does_not_advance_checkpoint(archive):
    archive.seed_json(date(2026, 10, 30), date(2026, 10, 30))
    job = archive.db.execute("select * from api_jobs limit 1").fetchone()
    client = client_for(
        archive.root, lambda _: httpx.Response(200, json={"data": [], "pagination_key": "same"})
    )
    try:
        archive.fetch_json_page(client, job)
        job = archive.db.execute("select * from api_jobs where id=?", (job["id"],)).fetchone()
        with pytest.raises(ValueError, match="pagination_cycle"):
            archive.fetch_json_page(client, job)
        assert (
            archive.db.execute(
                "select next_page from api_jobs where id=?", (job["id"],)
            ).fetchone()[0]
            == 1
        )
    finally:
        client.close()


def test_empty_response_is_complete_with_zero_rows(archive):
    archive.seed_json(date(2026, 10, 30), date(2026, 10, 30))
    job = archive.db.execute("select * from api_jobs limit 1").fetchone()
    client = client_for(archive.root, lambda _: httpx.Response(200, json={"data": []}))
    try:
        archive.fetch_json_page(client, job)
        state = archive.db.execute(
            "select state,rows from api_jobs where id=?", (job["id"],)
        ).fetchone()
        assert tuple(state) == ("complete", 0)
        assert archive.db.execute("select count(*) from api_pages").fetchone()[0] == 1
    finally:
        client.close()


def test_429_waits_and_error_text_never_exposes_secret(tmp_path):
    responses = [httpx.Response(429), httpx.Response(200, json={"data": []})]
    client = client_for(tmp_path, lambda _: responses.pop(0))
    waits = []
    client.wait = waits.append
    try:
        payload, _ = client.get("/edinet/major-shareholders", {"date": "2026-10-30"})
        assert payload == {"data": []} and 120 in waits
        with pytest.raises(ValueError, match="endpoint_not_allowed"):
            client.get("/send-order", {})
    finally:
        client.close()


def test_stop_prevents_network(tmp_path):
    (tmp_path / "STOP").touch()
    client = client_for(tmp_path, lambda _: pytest.fail("unexpected network"))
    try:
        with pytest.raises(FetchError, match="stop_requested"):
            client.get("/bulk/list", {"endpoint": "/equities/bars/daily"})
    finally:
        client.close()


@pytest.mark.parametrize("payload", [{}, {"data": [1]}, {"data": [], "pagination_key": 1}])
def test_invalid_payload(payload):
    with pytest.raises(ValueError):
        validate_rows(payload)


def test_file_versions_have_distinct_identifiers():
    assert identity(["key", "modified-1", 1]) != identity(["key", "modified-2", 1])


def test_raw_corruption_is_not_overwritten(archive):
    raw, _ = archive.immutable(b"original")
    (archive.root / raw).write_bytes(b"changed")
    with pytest.raises(ValueError, match="existing_raw_corrupt"):
        archive.immutable(b"original")
    assert (archive.root / raw).read_bytes() == b"changed"


def test_audit_reports_corruption_without_modifying_sources(archive):
    from trading_system.history_audit import audit_history

    archive.seed_json(date(2026, 10, 30), date(2026, 10, 30))
    job = archive.db.execute("select * from api_jobs limit 1").fetchone()
    client = client_for(archive.root, lambda _: httpx.Response(200, json={"data": []}))
    try:
        archive.fetch_json_page(client, job)
    finally:
        client.close()
    first = audit_history(archive.root)
    assert first["state"] == "checked" and first["pages_checked"] == 1
    raw = archive.db.execute("select raw_path from api_pages").fetchone()[0]
    (archive.root / raw).write_bytes(b"corrupt")
    second = audit_history(archive.root)
    assert second["state"] == "issues_found"
    assert (archive.root / raw).read_bytes() == b"corrupt"


def test_page_failure_does_not_commit_records_or_checkpoint(archive):
    archive.seed_json(date(2026, 10, 30), date(2026, 10, 30))
    job = archive.db.execute("select * from api_jobs limit 1").fetchone()
    archive.db.execute(
        "CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON api_jobs "
        "BEGIN SELECT RAISE(ABORT,'injected failure'); END"
    )
    client = client_for(
        archive.root,
        lambda _: httpx.Response(200, json={"data": [{"DocId": "doc", "Code": "01230"}]}),
    )
    try:
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            archive.fetch_json_page(client, job)
        assert archive.db.execute("select count(*) from api_records").fetchone()[0] == 0
        assert archive.db.execute("select count(*) from api_pages").fetchone()[0] == 0
        assert (
            archive.db.execute(
                "select next_page from api_jobs where id=?", (job["id"],)
            ).fetchone()[0]
            == 0
        )
        archive.db.execute("DROP TRIGGER reject_checkpoint")
        archive.fetch_json_page(client, job)
        assert archive.db.execute("select count(*) from api_records").fetchone()[0] == 1
    finally:
        client.close()


def test_revised_file_is_retained_but_not_double_counted(archive):
    from trading_system.history_audit import audit_history

    body = gzip.compress(b"Date,Code,O,H,L,C,Vo,Va\n2020-01-01,01230,100,102,99,101,100,10100\n")
    meta = {
        "Key": "equities/bars/daily/historical/2020/test.csv.gz",
        "LastModified": "2026-10-30T00:00:00+00:00",
        "Size": len(body),
    }

    def api(request):
        if request.url.path == "/v2/bulk/list":
            return httpx.Response(200, json={"data": [dict(meta)]})
        return httpx.Response(200, json={"url": "https://bucket.r2.cloudflarestorage.com/file"})

    client = client_for(
        archive.root, api, lambda _: httpx.Response(200, stream=httpx.ByteStream(body))
    )
    try:
        archive.discover(client, "/equities/bars/daily")
        archive.fetch_bulk(client, archive.db.execute("select * from bulk_files").fetchone())
        meta["LastModified"] = "2026-10-31T00:00:00+00:00"
        archive.discover(client, "/equities/bars/daily")
        archive.fetch_bulk(
            client, archive.db.execute("select * from bulk_files where state='pending'").fetchone()
        )
        result = audit_history(archive.root)
        assert result["files_checked"] == 2
        assert result["daily_prices"]["rows"] == 1
        assert result["daily_prices"]["duplicate_code_dates"] == 0
        assert result["state"] == "checked"
    finally:
        client.close()
