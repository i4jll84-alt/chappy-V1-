import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from trading_system.data_sources.jquants import APIError, JQuantsClient
from trading_system.market_financial import (
    MarketFinancialStore,
    ingest_market_financial,
    market_financial_query,
)

OBSERVED = datetime(2026, 10, 23, tzinfo=timezone.utc)
TOPIX = {"from": "2026-10-21", "to": "2026-10-22"}
FINANCIALS = {"code": "86970"}


@pytest.fixture
def tmp_path(tmp_path_factory):
    # Keep Windows paths below MAX_PATH despite immutable filename/temp suffixes.
    return tmp_path_factory.mktemp("mf")


def topix(**changes):
    return {"Date": "2026-10-22", "O": 100, "H": 102, "L": 99, "C": 101} | changes


def financial(**changes):
    return {
        "Code": "86970",
        "DiscDate": "2026-10-22",
        "DiscTime": "15:00:00",
        "DiscNo": "20261022000001",
        "DocType": "ForecastRevision",
        "CurPerType": "FY",
        "CurPerSt": "2026-04-01",
        "CurPerEn": "2027-03-31",
        "CurFYSt": "2026-04-01",
        "CurFYEn": "2027-03-31",
        "Sales": "1000000000",
        "OP": "-100",
        "OdP": "",
        "NP": "0",
        "EPS": "0.12345678901234567890123456789",
    } | changes


def api(dataset, handler):
    return JQuantsClient(
        "test-secret",
        endpoint=dataset,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def ingest(root, rows, dataset="topix", refresh=False):
    with api(dataset, lambda _: httpx.Response(200, json={"data": rows})) as client:
        return ingest_market_financial(
            root, client, dataset, TOPIX if dataset == "topix" else FINANCIALS, "standard", refresh
        )


def save(store, rows, observed=OBSERVED, cursor=None, job_id=None):
    if job_id is None:
        job_id = store.start(
            TOPIX if store.dataset == "topix" else FINANCIALS, "standard", refresh=True
        )["job_id"]
    body = {"data": rows, "pagination_key": cursor}
    store.save_page(job_id, body, json.dumps(body).encode(), observed)
    return job_id


@pytest.mark.parametrize(
    "dataset,kwargs",
    [
        ("topix", {}),
        ("topix", {"start": "2026-10-22", "end": "2026-10-21"}),
        ("topix", {"start": "2026-10-21", "end": "2026-10-22", "code": "8697"}),
        ("topix", {"start": "20261021", "end": "2026-10-22"}),
        ("financials", {}),
        ("financials", {"code": 8697}),
        ("financials", {"code": "8697", "start": "2026-10-21"}),
        ("unknown", {}),
    ],
)
def test_query_bounds(dataset, kwargs):
    with pytest.raises(ValueError):
        market_financial_query(dataset, **kwargs)


def test_financial_query_date_or_code():
    assert market_financial_query("financials", code="186a") == {"code": "186A0"}
    assert market_financial_query("financials", date="2026-10-22") == {"date": "2026-10-22"}
    assert market_financial_query("financials", code="86970", date="2026-10-22") == {
        "code": "86970",
        "date": "2026-10-22",
    }


@pytest.mark.parametrize("dataset,row", [("topix", topix()), ("financials", financial())])
def test_raw_parquet_repeat_and_cached_rebuild(tmp_path, dataset, row):
    result = ingest(tmp_path, [row | {"Unknown": {"retained": True}}], dataset)
    assert result["state"] == "complete" and result["versions"] == 1
    repeat = ingest(tmp_path, [row | {"Unknown": {"retained": True}}], dataset, refresh=True)
    assert repeat["versions"] == repeat["current_rows"] == 1
    with api(dataset, lambda _: pytest.fail("Cached query reached network")) as client:
        cached = ingest_market_financial(
            tmp_path, client, dataset, TOPIX if dataset == "topix" else FINANCIALS, "standard"
        )
    assert cached["cached"] and cached["job_id"] == repeat["job_id"]
    with MarketFinancialStore(tmp_path, dataset) as store:
        manifest = json.loads((store.root / "normalized/latest.json").read_bytes())
        for entry in manifest["files"].values():
            path = store.root / entry["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
            assert (
                store.db.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
                == entry["rows"]
            )
        payload, published, updated, available, observed = store.db.execute(
            "SELECT payload_json,published_at,source_updated_at,available_at,observed_at "
            f"FROM {store.versions}"
        ).fetchone()
        assert json.loads(payload)["Unknown"] == {"retained": True}
        assert updated is None and available > observed
        if dataset == "financials":
            assert published == datetime(2026, 10, 22, 6, tzinfo=timezone.utc)
            assert store.db.execute(
                'SELECT "OdP","NP","OP","EPS" FROM financial_current'
            ).fetchone() == (None, "0", "-100", row["EPS"])
        else:
            assert published is None


@pytest.mark.parametrize(
    "changes",
    [
        {"Date": None},
        {"Date": "2026-10-20"},
        {"O": None},
        {"H": True},
        {"L": -1},
        {"C": "101"},
        {"H": 100},
        {"O": None, "H": None, "L": None, "C": None},
    ],
)
def test_topix_bad_rows_retained(tmp_path, changes):
    result = ingest(tmp_path, [topix(**changes)])
    assert result["state"] == "quality_failed"
    with MarketFinancialStore(tmp_path, "topix") as store:
        assert len(list(store.root.glob("raw/*/*.meta.json"))) == 1
        assert store.as_of(datetime.now(timezone.utc) + timedelta(seconds=2)).num_rows == 0
        assert (
            store.db.execute(
                "SELECT count(*) FROM quality_issues WHERE severity='error'"
            ).fetchone()[0]
            > 0
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"Code": "12340"},
        {"Code": []},
        {"DiscNo": None},
        {"DiscNo": "not-number"},
        {"DocType": ""},
        {"DiscDate": "2026-99-99"},
        {"DiscTime": "25:00"},
        {"DiscTime": "15:00:01"},
        {"CurFYSt": "2028-01-01"},
        {"Sales": "NaN"},
        {"EPS": "Infinity"},
        {"NP": 12},
        {"CFO": []},
    ],
)
def test_financial_invalid_rows_retained(tmp_path, changes):
    result = ingest(tmp_path, [financial(**changes)], "financials")
    assert result["state"] == "quality_failed"
    with MarketFinancialStore(tmp_path, "financials") as store:
        assert store.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
        assert store.as_of(datetime.now(timezone.utc) + timedelta(seconds=2)).num_rows == 0


def test_unknown_publication_and_date_filter(tmp_path):
    assert ingest(tmp_path, [financial(DiscTime="")], "financials")["state"] == "complete"
    with MarketFinancialStore(tmp_path, "financials") as store:
        assert store.db.execute(
            "SELECT published_at,time_confidence FROM financial_current"
        ).fetchone() == (None, "observed_only")
        job = store.start({"date": "2026-10-21"}, "standard")
        save(store, [financial()], datetime.now(timezone.utc), job_id=job["job_id"])
        assert store.finish(job["job_id"])["state"] == "quality_failed"


@pytest.mark.parametrize("time", ["12:00", "14:00", "12:00:00", "14:00:00"])
def test_verified_financial_time_formats_have_minute_precision(tmp_path, time):
    result = ingest(tmp_path, [financial(DiscTime=time)], "financials")
    assert result["state"] == "complete"
    with MarketFinancialStore(tmp_path, "financials") as store:
        published, original, confidence = store.db.execute(
            "SELECT published_at,disclosure_time,time_confidence FROM financial_current"
        ).fetchone()
        assert published == datetime(2026, 10, 22, int(time[:2]) - 9, tzinfo=timezone.utc)
        assert original == time and confidence == "publication_minute_and_observed"


def test_time_format_requalification_retains_failed_snapshot(tmp_path, monkeypatch):
    import trading_system.market_financial as module

    rows = [financial(DiscTime="12:00"), financial(DiscNo="20261022000002")]
    original = module._publication
    monkeypatch.setattr(
        module,
        "_publication",
        lambda row: None if row.get("DiscTime") == "12:00" else original(row),
    )
    first = ingest(tmp_path, rows, "financials")
    assert first["state"] == "quality_failed" and first["versions"] == 2
    monkeypatch.setattr(module, "_publication", original)
    second = ingest(tmp_path, rows, "financials", refresh=True)
    assert second["state"] == "complete" and second["versions"] == 3
    with MarketFinancialStore(tmp_path, "financials") as store:
        assert store.job(first["job_id"])["state"] == "quality_failed"
        assert store.db.execute(
            "SELECT quality_status,available_at FROM financial_versions WHERE version_id=1"
        ).fetchone() == ("error", None)
        assert (
            store.db.execute(
                "SELECT count(*) FROM financial_current WHERE available_at IS NOT NULL"
            ).fetchone()[0]
            == 2
        )
        assert len(list(store.root.glob("raw/*/*.meta.json"))) == 2


def test_publication_after_observation_and_future_date(tmp_path):
    with MarketFinancialStore(tmp_path, "financials") as store:
        job_id = save(store, [financial(DiscDate="2026-10-23", DiscTime="10:00:00")])
        assert store.finish(job_id)["state"] == "quality_failed"
        assert (
            store.db.execute(
                "SELECT count(*) FROM quality_issues WHERE rule='publication_after_observation'"
            ).fetchone()[0]
            == 1
        )


def test_financial_disclosures_are_events_and_corrections_retain_versions(tmp_path):
    with MarketFinancialStore(tmp_path, "financials") as store:
        for index, sales in enumerate(["100", "200", "100"]):
            save(store, [financial(Sales=sales)], OBSERVED + timedelta(hours=index))
        rows = store.db.execute(
            "SELECT version_id,supersedes_version_id FROM financial_versions ORDER BY version_id"
        ).fetchall()
        assert rows == [(1, None), (2, 1), (3, 2)]
        save(
            store,
            [financial(DiscNo="20261022000000", DiscTime="16:00:00")],
            OBSERVED + timedelta(hours=3),
        )
        assert store.summary()["versions"] == 4 and store.summary()["current_rows"] == 2
        assert store.db.execute(
            "SELECT supersedes_version_id FROM financial_current "
            "WHERE disclosure_number='20261022000000'"
        ).fetchone() == (None,)


@pytest.mark.parametrize("dataset,row", [("topix", topix()), ("financials", financial())])
def test_duplicate_conflicts_across_pages_remain_quarantined(tmp_path, dataset, row):
    with MarketFinancialStore(tmp_path, dataset) as store:
        job_id = save(store, [row], cursor="next")
        other = row | ({"C": 100} if dataset == "topix" else {"Sales": "200"})
        save(store, [other, other], OBSERVED + timedelta(seconds=1), job_id=job_id)
        assert store.finish(job_id)["state"] == "quality_failed"
        assert (
            store.db.execute(
                "SELECT count(*) FROM quality_issues WHERE rule='duplicate_conflict'"
            ).fetchone()[0]
            == 2
        )
        assert (
            store.db.execute(
                f"SELECT count(*) FROM {store.versions} WHERE available_at IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        clean = save(store, [other], OBSERVED + timedelta(hours=1))
        assert store.finish(clean)["state"] == "complete"
        assert store.as_of(datetime.now(timezone.utc) + timedelta(seconds=2)).num_rows == 1


def test_identical_duplicate_does_not_add_version(tmp_path):
    result = ingest(tmp_path, [topix(), topix()])
    assert result["state"] == "complete" and result["versions"] == 1
    assert ("warning", "duplicate_identical", 1) in result["quality"]


def test_pit_waits_for_final_snapshot_and_empty_or_bad_refresh_hides_old(tmp_path, monkeypatch):
    import trading_system.market_financial as module

    clock = OBSERVED
    monkeypatch.setattr(module, "utcnow", lambda: clock)
    with MarketFinancialStore(tmp_path, "topix") as store:
        first = save(store, [topix()], cursor="next")
        assert store.as_of(clock + timedelta(days=1), TOPIX).num_rows == 0
        assert store.db.execute("SELECT available_at FROM topix_versions").fetchone()[0] is None
        with pytest.raises(ValueError, match="final page"):
            store.finish(first)
        clock += timedelta(hours=1)
        save(store, [topix(Date="2026-10-21")], clock, job_id=first)
        store.finish(first)
        assert store.as_of(clock, TOPIX).num_rows == 0
        assert store.as_of(clock + timedelta(seconds=1), TOPIX).num_rows == 2
        assert store.db.execute(
            "SELECT min(available_at),max(available_at) FROM topix_versions"
        ).fetchone() == (clock + timedelta(seconds=1), clock + timedelta(seconds=1))
        clock += timedelta(hours=1)
        second = save(store, [], clock)
        store.finish(second)
        assert store.as_of(clock + timedelta(seconds=1), TOPIX).num_rows == 0
        clock += timedelta(hours=1)
        third = save(store, [topix(O=None)], clock)
        store.finish(third)
        assert store.as_of(clock + timedelta(seconds=1), TOPIX).num_rows == 0


def test_failure_resumes_next_page_and_preserves_raw_bytes(tmp_path):
    seen = []
    raw = (
        b'{ "data" : [{"Date":"2026-10-22","O":100,"H":102,"L":99,"C":101}], '
        b'"pagination_key":"next" }\n'
    )

    def handler(request):
        seen.append(request.url.params.get("pagination_key"))
        return httpx.Response(401) if seen[-1] else httpx.Response(200, content=raw)

    with api("topix", handler) as client, pytest.raises(APIError):
        ingest_market_financial(tmp_path, client, "topix", TOPIX, "standard")
    with api(
        "topix", lambda request: httpx.Response(200, json={"data": [topix(Date="2026-10-21")]})
    ) as client:
        result = ingest_market_financial(tmp_path, client, "topix", TOPIX, "standard")
    assert result["state"] == "complete" and result["rows_received"] == 2
    assert seen == [None, "next"]
    with MarketFinancialStore(tmp_path, "topix") as store:
        path = store.db.execute("SELECT raw_path FROM pages ORDER BY page_number").fetchone()[0]
        assert (store.root / path).read_bytes() == raw


def test_page_checkpoint_rollback_and_cursor_cycle(tmp_path, monkeypatch):
    with MarketFinancialStore(tmp_path, "topix") as store:
        job = store.start(TOPIX, "standard")
        original = store._save_row

        def broken(*args):
            original(*args)
            raise RuntimeError("transaction failure")

        monkeypatch.setattr(store, "_save_row", broken)
        with pytest.raises(RuntimeError):
            save(store, [topix()], job_id=job["job_id"])
        assert store.job(job["job_id"])["pages"] == store.summary()["versions"] == 0
        monkeypatch.setattr(store, "_save_row", original)
        save(store, [topix()], cursor="next", job_id=job["job_id"])
        with pytest.raises(ValueError, match="cycle"):
            save(store, [topix()], cursor="next", job_id=job["job_id"])
        assert store.job(job["job_id"])["pages"] == 1
        assert store.db.execute("SELECT count(*) FROM rejected_pages").fetchone()[0] == 1


def test_export_failure_recovers_without_refetch(tmp_path, monkeypatch):
    original = MarketFinancialStore.export
    monkeypatch.setattr(
        MarketFinancialStore, "export", lambda _: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError):
        ingest(tmp_path, [topix()])
    monkeypatch.setattr(MarketFinancialStore, "export", original)
    with api("topix", lambda _: pytest.fail("Refetched completed pages")) as client:
        result = ingest_market_financial(tmp_path, client, "topix", TOPIX, "standard")
    assert result["state"] == "complete"


def test_endpoint_mismatch_rejected(tmp_path):
    with api("financials", lambda _: pytest.fail("Wrong endpoint reached HTTP")) as client:
        with pytest.raises(ValueError, match="endpoint"):
            ingest_market_financial(tmp_path, client, "topix", TOPIX, "standard")
