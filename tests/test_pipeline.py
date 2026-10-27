import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from trading_system.data_sources.jquants import APIError, JQuantsClient
from trading_system.ingestion.prices import incremental_start, ingest_prices, price_query
from trading_system.storage.store import Store

PARAMS = price_query("186A", "2026-10-01", "2026-10-20")
OBSERVED = datetime(2026, 10, 23, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "prices") as result:
        yield result


def bar(**changes):
    row = {
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
    }
    return row | changes


def api(handler):
    return JQuantsClient(
        "test-secret", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )


def ingest(store, rows, refresh=False):
    with api(lambda _: httpx.Response(200, json={"data": rows})) as client:
        return ingest_prices(store, client, PARAMS, "standard", refresh=refresh)


def count(store, table):
    return store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def save(store, row, when):
    job = store.start(PARAMS, "standard", refresh=True)
    body = {"data": [row]}
    store.save_page(job["job_id"], body, json.dumps(body).encode(), when)
    finish(store, job["job_id"], when)
    return job["job_id"]


def finish(store, job_id, when):
    with patch("trading_system.storage.store.utcnow", return_value=when):
        return store.finish(job_id)


def test_nonempty_parquet_is_read_and_manifest_hash_verified(store):
    result = ingest(store, [bar()])
    assert result["state"] == "complete"
    assert result["verified_duckdb_read"] is True
    assert store.db.execute("SELECT status,outcome FROM attempts").fetchall() == [(200, "success")]
    manifest = json.loads((store.root / "normalized/latest.json").read_text())
    for name, entry in manifest["files"].items():
        path = store.root / entry["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        assert (
            store.db.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
            == entry["rows"]
        )
        if name == "price_versions":
            assert entry["rows"] == 1


def test_repeat_and_refresh_do_not_duplicate_versions(store):
    first = ingest(store, [bar()])
    with api(lambda _: pytest.fail("cached job must not make HTTP requests")) as client:
        cached = ingest_prices(store, client, PARAMS, "standard")
    assert cached["cached"] is True
    assert cached["job_id"] == first["job_id"]
    refreshed = ingest(store, [bar()], refresh=True)
    assert refreshed["job_id"] != first["job_id"]
    assert count(store, "price_versions") == 1
    assert count(store, "observations") == 2


def test_correction_history_and_as_of_boundary(store):
    save(store, bar(), OBSERVED)
    later = OBSERVED + timedelta(hours=1)
    save(store, bar(C=106, AdjC=106), later)
    assert store.as_of(OBSERVED).num_rows == 0
    assert store.as_of(OBSERVED + timedelta(seconds=1))["close"].to_pylist() == [105]
    assert store.as_of(later)["close"].to_pylist() == [105]
    assert store.as_of(later + timedelta(seconds=1))["close"].to_pylist() == [106]
    versions = store.db.execute(
        "SELECT version_id,supersedes_version_id,published_at,source_updated_at "
        "FROM price_versions ORDER BY version_id"
    ).fetchall()
    assert versions[1][1] == versions[0][0]
    assert all(v[2:] == (None, None) for v in versions)


def test_a_b_a_is_three_versions(store):
    for index, close in enumerate([105, 106, 105]):
        save(store, bar(C=close, AdjC=close), OBSERVED + timedelta(hours=index))
    versions = store.db.execute(
        "SELECT close,content_hash FROM price_versions ORDER BY version_id"
    ).fetchall()
    assert [v[0] for v in versions] == [105, 106, 105]
    assert versions[0][1] == versions[2][1]
    assert count(store, "prices_current") == 1


def test_second_page_failure_resumes_cursor_without_refetch(store):
    requests = []

    def handler(request):
        cursor = request.url.params.get("pagination_key")
        requests.append(cursor)
        if cursor is None:
            return httpx.Response(200, json={"data": [bar()], "pagination_key": "next"})
        return httpx.Response(401)

    with api(handler) as client, pytest.raises(APIError):
        ingest_prices(store, client, PARAMS, "standard")
    job_id = store.db.execute("SELECT job_id FROM jobs").fetchone()[0]
    assert store.job(job_id)["pages"] == 1
    assert store.job(job_id)["cursor"] == "next"

    def resumed(request):
        requests.append(request.url.params.get("pagination_key"))
        return httpx.Response(200, json={"data": [bar(Date="2026-10-02")]})

    with api(resumed) as client:
        result = ingest_prices(store, client, PARAMS, "standard")
    assert result["state"] == "complete"
    assert requests == [None, "next", "next"]
    assert store.job(job_id)["pages"] == 2
    assert count(store, "price_versions") == 2


def test_export_failure_resumes_without_http(store, monkeypatch):
    original = store.export

    def broken_export():
        raise OSError("simulated export failure")

    monkeypatch.setattr(store, "export", broken_export)
    with pytest.raises(OSError):
        ingest(store, [bar()])
    job_id = store.db.execute("SELECT job_id FROM jobs").fetchone()[0]
    assert store.job(job_id)["error_category"] == "export_failed"
    monkeypatch.setattr(store, "export", original)
    with api(lambda _: pytest.fail("export recovery must not refetch")) as client:
        result = ingest_prices(store, client, PARAMS, "standard")
    assert result["state"] == "complete"
    assert count(store, "price_versions") == 1


def test_raw_response_bytes_and_sha_are_exact(store):
    raw = ('{ "data" : [ ' + json.dumps(bar()) + ' ], "unknown": "\\u65e5" }\n').encode()
    with api(lambda _: httpx.Response(200, content=raw)) as client:
        ingest_prices(store, client, PARAMS, "standard")
    path, sha = store.db.execute("SELECT raw_path,raw_sha256 FROM pages").fetchone()
    assert (store.root / path).read_bytes() == raw
    assert sha == hashlib.sha256(raw).hexdigest()


def test_pagination_cycle_rejected_and_checkpoint_kept(store):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [bar()], "pagination_key": "same"})

    with api(handler) as client, pytest.raises(ValueError, match="cycle"):
        ingest_prices(store, client, PARAMS, "standard")
    assert len(requests) == 2
    assert count(store, "pages") == 1
    assert count(store, "observations") == 1
    assert store.db.execute("SELECT cursor FROM jobs").fetchone()[0] == "same"
    metadata_files = list((store.root / "raw").rglob("*.meta.json"))
    response_files = [
        path
        for path in (store.root / "raw").rglob("*.json")
        if not path.name.endswith(".meta.json")
    ]
    assert len(response_files) == len(metadata_files) == 2
    assert all(json.loads(path.read_bytes())["pagination_key"] == "same" for path in response_files)
    rejected = [
        json.loads(path.read_bytes())
        for path in metadata_files
        if "rejection_reason" in json.loads(path.read_bytes())
    ]
    assert len(rejected) == 1
    assert rejected[0]["rejection_reason"] == "pagination_cursor_cycle"
    assert rejected[0]["request_cursor"] == "same"
    assert rejected[0]["page_number"] == 1
    rejected_bytes = (store.root / rejected[0]["raw_path"]).read_bytes()
    assert rejected[0]["raw_sha256"] == hashlib.sha256(rejected_bytes).hexdigest()


def test_empty_is_distinct_from_quality_failure(store):
    assert ingest(store, [])["state"] == "empty"
    result = ingest(store, [bar(C=-1)], refresh=True)
    assert result["state"] == "quality_failed"
    assert result["quality_errors"] > 0


@pytest.mark.parametrize(
    "changes",
    [{"C": -1}, {"C": None}, {"Date": "2026-09-30"}, {"Date": "2026-02-30"}, {"Code": "13010"}],
)
def test_invalid_input_remains_in_raw_and_is_excluded_from_as_of(store, changes):
    row = bar(**changes)
    before = copy.deepcopy(row)
    assert ingest(store, [row])["state"] == "quality_failed"
    path = store.db.execute("SELECT raw_path FROM pages").fetchone()[0]
    assert json.loads((store.root / path).read_bytes())["data"][0] == before
    assert count(store, "observations") == 1
    assert store.as_of(datetime(2099, 1, 1, tzinfo=timezone.utc)).num_rows == 0


def test_incremental_overlap_uses_completed_query_end_not_last_row(store):
    assert incremental_start(store, "186A", "2026-09-01", "2026-10-30") == "2026-09-01"
    ingest(store, [bar()])
    assert incremental_start(store, "186A", "2026-09-01", "2026-10-30") == "2026-09-01"
    assert incremental_start(store, "186A", "2026-10-01", "2026-10-30") == "2026-10-13"
    assert incremental_start(store, "186A", "2026-10-15", "2026-10-30") == "2026-10-15"


def test_empty_and_quality_failed_jobs_do_not_advance_incremental_coverage(store):
    ingest(store, [])
    ingest(store, [bar(C=-1)], refresh=True)
    assert incremental_start(store, "186A", "2026-09-01", "2026-10-30") == "2026-09-01"


def test_page_transaction_rolls_back_rows_issues_and_checkpoint(store, monkeypatch):
    job = store.start(PARAMS, "standard")
    connection = store.db

    class FaultyConnection:
        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO pages"):
                raise RuntimeError("injected failure after row insertion")
            return connection.execute(sql, *args)

    monkeypatch.setattr(store, "db", FaultyConnection())
    body = {"data": [bar(C=-1)], "pagination_key": "next"}
    with pytest.raises(RuntimeError, match="injected failure"):
        store.save_page(job["job_id"], body, json.dumps(body).encode(), OBSERVED)
    monkeypatch.setattr(store, "db", connection)
    for table in ("price_versions", "observations", "quality_issues", "pages"):
        assert count(store, table) == 0
    checkpoint = store.job(job["job_id"])
    assert checkpoint["pages"] == checkpoint["rows_received"] == 0
    assert checkpoint["cursor"] is None
    store.save_page(job["job_id"], body, json.dumps(body).encode(), OBSERVED)
    assert count(store, "pages") == 1


def test_incremental_coverage_merges_adjacent_intervals_but_stops_at_gap(store):
    for start, end in [
        ("2026-09-01", "2026-09-10"),
        ("2026-09-11", "2026-09-20"),
        ("2026-09-22", "2026-10-20"),
    ]:
        params = price_query("186A", start, end)
        with api(
            lambda _, day=start: httpx.Response(200, json={"data": [bar(Date=day)]})
        ) as client:
            assert ingest_prices(store, client, params, "standard")["state"] == "complete"
    assert incremental_start(store, "186A", "2026-09-01", "2026-10-30") == "2026-09-13"
    params = price_query("186A", "2026-09-21", "2026-09-21")
    with api(lambda _: httpx.Response(200, json={"data": [bar(Date="2026-09-21")]})) as client:
        ingest_prices(store, client, params, "standard")
    assert incremental_start(store, "186A", "2026-09-01", "2026-10-30") == "2026-10-13"


def test_four_and_five_character_code_have_one_current_key(store):
    save(store, bar(Code="186A"), OBSERVED)
    save(store, bar(Code="186A0"), OBSERVED + timedelta(hours=1))
    assert count(store, "prices_current") == 1
    assert store.db.execute("SELECT code FROM prices_current").fetchone()[0] == "186A0"


def test_jobs_parquet_catalog_and_receipt_terminal_states_agree(store):
    result = ingest(store, [bar()])
    manifest = json.loads((store.root / "normalized/latest.json").read_text())
    path = str(store.root / manifest["files"]["jobs"]["path"])
    parquet_state = store.db.execute(
        "SELECT state FROM read_parquet(?) WHERE job_id=?", [path, result["job_id"]]
    ).fetchone()[0]
    receipt = json.loads((store.root / "logs" / f"{result['job_id']}.json").read_text())
    assert parquet_state == store.job(result["job_id"])["state"] == receipt["state"] == "complete"


def test_same_payload_quality_context_changes_create_pit_versions(store):
    body = {"data": [bar()]}
    raw = json.dumps(body).encode()
    for index, query_code in enumerate(["1301", "186A", "1301"]):
        params = price_query(query_code, PARAMS["from"], PARAMS["to"])
        job = store.start(params, "standard", refresh=True)
        observed = OBSERVED + timedelta(hours=index)
        store.save_page(job["job_id"], body, raw, observed)
        finish(store, job["job_id"], observed)
        visible = store.as_of(observed + timedelta(seconds=1))
        assert visible.num_rows == (1 if index == 1 else 0)
    assert count(store, "price_versions") == 3
    assert store.db.execute(
        "SELECT quality_status FROM price_versions ORDER BY version_id"
    ).fetchall() == [("error",), ("ok",), ("error",)]
    assert store.as_of(OBSERVED + timedelta(minutes=30)).num_rows == 0
    assert store.as_of(OBSERVED + timedelta(hours=1, minutes=30)).num_rows == 1


@pytest.mark.parametrize("numeric_text", ["1e309", str(10**400)])
def test_extreme_number_is_retained_and_rejects_latest_version_without_fallback(
    store, numeric_text
):
    save(store, bar(), OBSERVED - timedelta(days=1))
    payload = {"data": [bar(C="EXTREME_NUMBER")]}
    raw = json.dumps(payload).replace('"EXTREME_NUMBER"', numeric_text).encode()
    with api(lambda _: httpx.Response(200, content=raw)) as client:
        result = ingest_prices(store, client, PARAMS, "standard", refresh=True)
    assert result["state"] == "quality_failed"
    assert result["rows_received"] == 1
    assert count(store, "observations") == 2
    path = store.db.execute(
        "SELECT raw_path FROM pages WHERE job_id=?", [result["job_id"]]
    ).fetchone()[0]
    assert (store.root / path).read_bytes() == raw
    close, quality, normalized = store.db.execute(
        "SELECT close,quality_status,payload_json FROM prices_current"
    ).fetchone()
    assert close is None
    assert quality == "error"
    normalized_row = json.loads(normalized)
    if numeric_text == "1e309":
        assert "$invalid_number" in normalized_row["C"]
    else:
        assert normalized_row["C"] == 10**400
    assert store.as_of(datetime(2099, 1, 1, tzinfo=timezone.utc)).num_rows == 0


def test_retry_attempts_are_recorded_for_429_then_success(store):
    responses = iter([httpx.Response(429), httpx.Response(200, json={"data": [bar()]})])
    with api(lambda _: next(responses)) as client:
        assert ingest_prices(store, client, PARAMS, "standard")["state"] == "complete"
    assert store.db.execute(
        "SELECT status,outcome FROM attempts ORDER BY attempted_at"
    ).fetchall() == [(429, "rate_limited"), (200, "success")]


@pytest.mark.parametrize("closes", [(105, 106, 106), (105, 106, 105)], ids=["A-B-B", "A-B-A"])
@pytest.mark.parametrize("page_count", [1, 3], ids=["one-page", "three-pages"])
def test_conflicting_duplicates_stay_quarantined_until_new_refresh(store, closes, page_count):
    job = store.start(PARAMS, "standard")
    rows = [bar(C=close, AdjC=close) for close in closes]
    pages = [rows] if page_count == 1 else [[row] for row in rows]
    for index, page_rows in enumerate(pages):
        body = {"data": page_rows}
        if index + 1 < len(pages):
            body["pagination_key"] = f"page-{index + 1}"
        store.save_page(
            job["job_id"], body, json.dumps(body).encode(), OBSERVED + timedelta(minutes=index)
        )
    assert (
        finish(store, job["job_id"], OBSERVED + timedelta(minutes=2))["state"] == "quality_failed"
    )
    assert store.db.execute("SELECT quality_status FROM prices_current").fetchone()[0] == "error"
    conflicts = store.db.execute(
        "SELECT count(*) FROM quality_issues WHERE job_id=? "
        "AND rule='duplicate_conflict' AND severity='error'",
        [job["job_id"]],
    ).fetchone()[0]
    assert conflicts == 2
    after_conflicts = OBSERVED + timedelta(minutes=3)
    assert store.as_of(after_conflicts).num_rows == 0

    refreshed = store.start(PARAMS, "standard", refresh=True)
    recovery_at = OBSERVED + timedelta(hours=1)
    body = {"data": [rows[-1]]}
    store.save_page(refreshed["job_id"], body, json.dumps(body).encode(), recovery_at)
    assert finish(store, refreshed["job_id"], recovery_at)["state"] == "complete"
    assert store.db.execute("SELECT quality_status FROM prices_current").fetchone()[0] == "ok"
    assert store.as_of(recovery_at).num_rows == 0
    assert store.as_of(recovery_at + timedelta(seconds=1))["close"].to_pylist() == [closes[-1]]
    assert store.as_of(after_conflicts).num_rows == 0


def test_snapshot_waits_for_all_pages_and_completion_boundary(store):
    job_id = store.start(PARAMS, "standard")["job_id"]
    first = {"data": [bar()], "pagination_key": "next"}
    store.save_page(job_id, first, json.dumps(first).encode(), OBSERVED)
    assert store.as_of(OBSERVED + timedelta(minutes=10)).num_rows == 0
    store.fail(job_id, "ingestion_failed")
    assert store.as_of(OBSERVED + timedelta(minutes=10)).num_rows == 0
    second = {"data": [bar(Date="2026-10-02")]}
    store.save_page(job_id, second, json.dumps(second).encode(), OBSERVED + timedelta(minutes=1))
    assert store.as_of(OBSERVED + timedelta(minutes=10)).num_rows == 0
    completed = OBSERVED + timedelta(minutes=2)
    finish(store, job_id, completed)
    assert store.as_of(completed).num_rows == 0
    visible = store.as_of(completed + timedelta(seconds=1))
    assert visible["close"].to_pylist() == [105, 105]
    assert visible["available_at"].to_pylist() == [completed + timedelta(seconds=1)] * 2
    assert visible["snapshot_job_id"].to_pylist() == [job_id] * 2
    assert visible["finalization_source"].to_pylist() == ["terminal_updated_at"] * 2
    assert (
        visible["availability_rule"].to_pylist() == ["job_terminal_updated_at_plus_1_second_v1"] * 2
    )
    # Original legacy row evidence is immutable, including its observation bound.
    assert store.db.execute(
        "SELECT available_at FROM price_versions ORDER BY version_id"
    ).fetchall() == [
        (OBSERVED + timedelta(seconds=1),),
        (OBSERVED + timedelta(minutes=1, seconds=1),),
    ]


def test_snapshot_membership_and_exact_query_never_union_old_rows(store):
    first = save(store, bar(), OBSERVED)
    second = save(store, bar(Date="2026-10-02"), OBSERVED + timedelta(minutes=1))
    decision = OBSERVED + timedelta(hours=1)
    assert store.as_of(decision)["target_date"].to_pylist() == [datetime(2026, 10, 2).date()]
    # A failed or incomplete refresh contributes no rows to the last finalized snapshot.
    job_id = store.start(PARAMS, "standard", refresh=True)["job_id"]
    body = {"data": [bar(C=109, AdjC=109)], "pagination_key": "next"}
    store.save_page(job_id, body, json.dumps(body).encode(), OBSERVED + timedelta(minutes=2))
    for fail in (False, True):
        if fail:
            store.fail(job_id, "ingestion_failed")
        assert store.as_of(decision)["snapshot_job_id"].to_pylist() == [second]
    other = price_query("1301", PARAMS["from"], PARAMS["to"])
    other_id = store.start(other, "standard", refresh=True)["job_id"]
    body = {"data": [bar(Code="13010")]}
    store.save_page(other_id, body, json.dumps(body).encode(), OBSERVED + timedelta(minutes=3))
    finish(store, other_id, OBSERVED + timedelta(minutes=3))
    assert store.as_of(decision)["code"].to_pylist() == ["13010"]
    assert store.as_of(decision, PARAMS)["snapshot_job_id"].to_pylist() == [second]
    assert first != second


@pytest.mark.parametrize("rows,terminal", [([], "empty"), ([bar(C=-1)], "quality_failed")])
def test_finalized_nonclean_snapshot_masks_previous_clean_snapshot(store, rows, terminal):
    save(store, bar(), OBSERVED)
    job_id = store.start(PARAMS, "standard", refresh=True)["job_id"]
    body = {"data": rows}
    moment = OBSERVED + timedelta(minutes=1)
    store.save_page(job_id, body, json.dumps(body).encode(), moment)
    assert finish(store, job_id, moment)["state"] == terminal
    assert store.as_of(moment)["close"].to_pylist() == [105]
    assert store.as_of(moment + timedelta(seconds=1)).num_rows == 0


def test_snapshot_respects_later_stored_row_availability(store):
    job_id = save(store, bar(), OBSERVED)
    later = OBSERVED + timedelta(minutes=5)
    store.db.execute("UPDATE price_versions SET available_at=?", [later])
    assert store.as_of(later - timedelta(microseconds=1)).num_rows == 0
    assert store.as_of(later)["snapshot_job_id"].to_pylist() == [job_id]


@pytest.mark.parametrize("terminal", ["complete", "empty", "quality_failed"])
def test_terminal_job_evidence_cannot_be_mutated_by_lifecycle_methods(store, terminal):
    rows = {"complete": [bar()], "empty": [], "quality_failed": [bar(C=-1)]}[terminal]
    result = ingest(store, rows)
    job_id = result["job_id"]
    before = store.job(job_id)
    store.fail(job_id, "export_failed")
    with pytest.raises(ValueError, match="final page"):
        store.finish(job_id)
    body = {"data": [bar()]}
    with pytest.raises(ValueError, match="accepting pages"):
        store.save_page(job_id, body, json.dumps(body).encode(), OBSERVED)
    assert store.job(job_id) == before


@pytest.mark.parametrize("stage", ["fresh", "unfinished", "forged_export_failure"])
def test_finish_requires_saved_final_page(store, stage):
    job_id = store.start(PARAMS, "standard")["job_id"]
    if stage != "fresh":
        body = {"data": [bar()], "pagination_key": "next"}
        store.save_page(job_id, body, json.dumps(body).encode(), OBSERVED)
    if stage == "forged_export_failure":
        store.fail(job_id, "export_failed")
        store.db.execute("UPDATE jobs SET cursor=NULL WHERE job_id=?", [job_id])
    before = store.job(job_id)
    with pytest.raises(ValueError, match="final page"):
        store.finish(job_id)
    assert store.job(job_id) == before
    assert store.as_of(datetime(2099, 1, 1, tzinfo=timezone.utc)).num_rows == 0


def test_finish_completion_cannot_precede_observed_pages(store):
    job_id = store.start(PARAMS, "standard")["job_id"]
    body = {"data": [bar()]}
    store.save_page(job_id, body, json.dumps(body).encode(), OBSERVED)
    finish(store, job_id, OBSERVED - timedelta(hours=1))
    assert store.job(job_id)["updated_at"] == OBSERVED
    assert store.as_of(OBSERVED).num_rows == 0


def test_cached_ingestion_rebuilds_deleted_parquet_without_network_or_job_mutation(store):
    result = ingest(store, [bar()])
    job = store.job(result["job_id"])
    manifest = json.loads((store.root / "normalized/latest.json").read_bytes())
    (store.root / manifest["files"]["price_versions"]["path"]).unlink()
    (store.root / "normalized/latest.json").write_text('{"interrupted":true}')
    with api(lambda _: pytest.fail("cached recovery must not access network")) as client:
        cached = ingest_prices(store, client, PARAMS, "standard")
    assert cached["cached"] and cached["verified_duckdb_read"]
    assert cached["parquet_generation"] != manifest["generation"]
    rebuilt = json.loads((store.root / "normalized/latest.json").read_bytes())
    assert rebuilt["files"]["observations"]["rows"] == 1
    assert rebuilt["files"]["attempts"]["rows"] == 1
    path = store.root / rebuilt["files"]["price_versions"]["path"]
    assert store.db.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0] == 1
    assert store.job(result["job_id"]) == job


@pytest.mark.parametrize(
    "mode", ["success", "cached", "http_error", "export_error", "cached_export_error"]
)
def test_ingestion_restores_client_callback_on_every_exit(store, monkeypatch, mode):
    if mode.startswith("cached"):
        result = ingest(store, [bar()])
        terminal_before = store.job(result["job_id"])
    if "export_error" in mode:

        def broken_export():
            raise OSError("simulated export failure")

        monkeypatch.setattr(store, "export", broken_export)

    def callback(**_):
        return None

    status = 401 if mode == "http_error" else 200
    with api(lambda _: httpx.Response(status, json={"data": [bar()]})) as client:
        client.on_attempt = callback
        if "error" in mode:
            expected = APIError if mode == "http_error" else OSError
            with pytest.raises(expected):
                ingest_prices(store, client, PARAMS, "standard")
        else:
            ingest_prices(store, client, PARAMS, "standard")
        assert client.on_attempt is callback
    if mode == "cached_export_error":
        assert store.job(result["job_id"]) == terminal_before


def test_identical_refresh_uses_new_job_completion_without_rewriting_version(store):
    first = save(store, bar(), OBSERVED)
    stored_before = store.db.execute("SELECT * FROM price_versions").fetchall()
    later = OBSERVED + timedelta(hours=1)
    second = save(store, bar(), later)
    assert store.db.execute("SELECT * FROM price_versions").fetchall() == stored_before
    assert store.as_of(later)["snapshot_job_id"].to_pylist() == [first]
    refreshed = store.as_of(later + timedelta(seconds=1))
    assert refreshed["snapshot_job_id"].to_pylist() == [second]
    assert refreshed["available_at"].to_pylist() == [later + timedelta(seconds=1)]


def test_mixed_quality_snapshot_excludes_all_rows_without_older_fallback(store):
    save(store, bar(), OBSERVED)
    job_id = store.start(PARAMS, "standard", refresh=True)["job_id"]
    moment = OBSERVED + timedelta(minutes=1)
    body = {"data": [bar(Date="2026-10-02"), bar(C=-1)]}
    store.save_page(job_id, body, json.dumps(body).encode(), moment)
    assert finish(store, job_id, moment)["state"] == "quality_failed"
    assert store.as_of(moment + timedelta(seconds=1)).num_rows == 0


def test_receipt_write_failure_keeps_committed_job_completion(store, monkeypatch):
    from trading_system.storage import store as storage

    original = storage.atomic_write

    def fail_receipt(path, content):
        if path.parent == store.root / "logs":
            raise OSError("simulated receipt failure")
        return original(path, content)

    monkeypatch.setattr(storage, "atomic_write", fail_receipt)
    with pytest.raises(OSError, match="receipt failure"):
        ingest(store, [bar()])
    job_id = store.db.execute("SELECT job_id FROM jobs").fetchone()[0]
    committed = store.job(job_id)
    assert committed["state"] == "complete"
    assert committed["error_category"] is None
    assert store.as_of(committed["updated_at"] + timedelta(seconds=1)).num_rows == 1
    with api(lambda _: pytest.fail("committed job must not refetch")) as client:
        assert ingest_prices(store, client, PARAMS, "standard")["cached"]
    assert store.job(job_id) == committed
