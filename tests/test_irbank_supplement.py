"""Synthetic-only checks of fixed supplementary scope, schema, and immutable resume."""

import json

import duckdb
import httpx
import pytest

from trading_system.research import irbank_jsf as jsf
from trading_system.research import irbank_supplement as m


def fixture_manifest(root, codes=("7203",), liquid=("7203",)):
    jsf.save(root / "universe.json", {"codes": list(codes)})
    jobs = {
        "weekly": [{"id": code, "code": code} for code in codes],
        "financials": [{"id": code, "code": code} for code in liquid],
        "shareholders": [{"id": code, "code": code} for code in liquid],
        "masters": [{"id": "securities"}, {"id": "industries"}],
    }
    value = {
        "mode": m.MODE,
        "jobs": jobs,
        "jobs_sha256": m.signature(jobs),
        "universe_sha256": m.sha(root / "universe.json"),
    }
    m._freeze(root / "supplement/manifest.json", value)
    return value


def archive(root):
    return m.Archive(root, m.sha(root / "supplement/manifest.json"))


def response(endpoint, code="7203", *, status=200, cursor=None):
    if status != 200:
        return httpx.Response(status, json={"error": {"code": "not_found"}})
    if endpoint.endswith("weekly-margin-balance"):
        data = {
            "security_code": code,
            "exchange": "XTKS",
            "period": "52w",
            "fields": [],
            "weeks": [{"report_date": "2026-11-20"}],
        }
    elif endpoint == "financials":
        data = {"edinet_code": "E02144", "fields": [], "periods": [{"fiscal_year": "2026/03"}]}
    elif endpoint == "shareholders":
        data = {
            "fields": [],
            "shareholders": [
                {
                    "holder_name": "Synthetic",
                    "holding_ratio": 5,
                    "shares_held": 100,
                    "previous_ratio": None,
                }
            ],
        }
    elif endpoint == "securities":
        data = {
            "securities": [
                {"code": code, "name": "Synthetic", "market": "Prime", "industry": "Other"}
            ],
            "next_cursor": cursor,
        }
    else:
        data = {
            "industries": [
                {"name": "Synthetic", "classification": "33", "code": "synthetic", "parent": None}
            ]
        }
    return httpx.Response(200, json=data)


class FakeClient:
    sent = 0
    calls = []
    allowance = 950

    def __init__(self, *args):
        self.sent = 0

    def get_resource(self, endpoint, params, **kwargs):
        if self.sent >= self.allowance:
            raise jsf.Halt("run_request_limit")
        self.sent += 1
        self.calls.append((endpoint, params.copy(), kwargs))
        return response(
            endpoint,
            params.get("security_code", endpoint.split("/")[1] if "/" in endpoint else "7203"),
        )

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    FakeClient.calls = []
    FakeClient.allowance = 950
    monkeypatch.setattr(m, "Client", FakeClient)
    monkeypatch.setattr(m, "load_key", lambda _: "test-only")
    return FakeClient


def test_phase_resume_never_refetches_cached_jobs(tmp_path, fake):
    fixture_manifest(tmp_path, codes=("7203", "9999"))
    fake.allowance = 1
    first = m.run(tmp_path, tmp_path, phase="weekly")
    assert (first["state"], first["requests_this_run"], first["complete"], first["remaining"]) == (
        "stopped",
        1,
        1,
        1,
    )
    second = m.run(tmp_path, tmp_path, phase="weekly")
    assert (second["state"], second["requests_this_run"], second["complete"]) == ("complete", 1, 2)
    third = m.run(tmp_path, tmp_path, phase="weekly")
    assert third["state"] == "complete" and third["requests_this_run"] == 0
    assert len(fake.calls) == 2
    assert all(params == {"period": "52w"} for _, params, _ in fake.calls)


def test_financials_request_full_annual_scope_and_shareholder_resolves_edinet(tmp_path, fake):
    fixture_manifest(tmp_path)
    before = m.run(tmp_path, tmp_path, phase="shareholders")
    assert before["reason"] == "supplement_financial_dependency_pending" and not fake.calls
    assert m.run(tmp_path, tmp_path, phase="financials")["state"] == "complete"
    assert fake.calls[0][1] == {"security_code": "7203", "period": "annual", "years": 30}
    assert m.run(tmp_path, tmp_path, phase="shareholders")["state"] == "complete"
    assert fake.calls[1][:2] == ("shareholders", {"edinet_code": "E02144"})
    receipt = list((tmp_path / "supplement/raw/shareholders").glob("*/*/receipt.json"))[0]
    saved = json.loads(receipt.read_bytes())
    assert saved["publication_timestamp"] is None and saved["current_holdings_only"] is True


def test_404_cache_is_empty_unobserved_and_unresolvable_shareholder_does_not_call(tmp_path, fake):
    fixture_manifest(tmp_path)
    a = archive(tmp_path)
    job = {"id": "7203", "code": "7203"}
    request = m.request_for("financials", job, a)
    a.write("financials", "7203", request, response("financials", status=404))
    for phase in ("financials", "shareholders"):
        report = m.run(tmp_path, tmp_path, phase=phase)
        assert (
            report["state"] == "complete"
            and report["empty"] == 1
            and report["requests_this_run"] == 0
        )
    assert not fake.calls
    state = json.loads((tmp_path / "supplement/state/shareholders/7203.json").read_bytes())
    assert state["reason"] == "financial_404_edinet_unresolved"


def test_securities_cursor_resume_cached_first_page(tmp_path):
    fixture_manifest(tmp_path)
    a = archive(tmp_path)
    job = {"id": "securities"}
    req = m.request_for("masters", job, a)
    a.write("masters", "securities", req, response("securities", cursor="opaque/cursor+1"))

    class Client:
        calls = []

        def get_resource(self, endpoint, params, **kwargs):
            self.calls.append((endpoint, params))
            return response("securities", code="9999")

    client = Client()
    state = a.work(client, "masters", job)
    assert state["rows"] == 2 and len(state["pages"]) == 2
    assert client.calls == [("securities", {"limit": 500, "cursor": "opaque/cursor+1"})]
    assert a.work(client, "masters", job) == state
    assert len(client.calls) == 1


@pytest.mark.parametrize("kind", ["raw", "receipt", "partial", "manifest", "universe"])
def test_hash_or_partial_cache_rejected_without_refetch(tmp_path, fake, kind):
    fixture_manifest(tmp_path)
    a = archive(tmp_path)
    job = {"id": "7203", "code": "7203"}
    req = m.request_for("weekly", job, a)
    a.write("weekly", "7203", req, response(req["endpoint"]))
    raw, meta = a.paths("weekly", "7203", req)
    if kind == "raw":
        raw.write_bytes(raw.read_bytes() + b" ")
    elif kind == "receipt":
        meta.write_bytes(meta.read_bytes() + b" ")
    elif kind == "partial":
        meta.unlink()
    elif kind == "manifest":
        path = tmp_path / "supplement/manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
    else:
        (tmp_path / "universe.json").write_text("{}")
    report = m.run(tmp_path, tmp_path, phase="weekly")
    assert report["state"] == "stopped" and report["requests_this_run"] == 0 and not fake.calls


@pytest.mark.parametrize(
    "endpoint,change",
    [
        ("securities/7203/weekly-margin-balance", {"security_code": "9999"}),
        (
            "securities/7203/weekly-margin-balance",
            {"weeks": [{"report_date": "2026-11-20"}, {"report_date": "2026-11-20"}]},
        ),
        ("financials", {"edinet_code": "invalid"}),
        ("financials", {"periods": [{"fiscal_year": "2026/13"}]}),
        ("shareholders", {"shareholders": {}}),
        ("shareholders", {"next_cursor": "undocumented"}),
        ("securities", {"next_cursor": "same"}),
    ],
)
def test_malformed_pages_saved_then_stop_without_refetch(tmp_path, endpoint, change):
    fixture_manifest(tmp_path)
    a = archive(tmp_path)
    resp = response(endpoint)
    data = resp.json()
    data.update(change)
    req = {
        "endpoint": endpoint,
        "params": {"limit": 500, "cursor": "same"} if endpoint == "securities" else {},
    }
    phase = "weekly" if "/" in endpoint else endpoint
    with pytest.raises((jsf.Halt, ValueError, TypeError)):
        a.write(phase, "7203", req, httpx.Response(200, json=data))
    with pytest.raises((jsf.Halt, ValueError, TypeError)):
        a.read(phase, "7203", req)
    assert a.paths(phase, "7203", req)[0].exists()


def test_pagination_cycle_stops(tmp_path):
    fixture_manifest(tmp_path)
    a = archive(tmp_path)

    class Client:
        def get_resource(self, endpoint, params, **kwargs):
            return response(endpoint, cursor="B" if params.get("cursor") != "B" else "A")

    with pytest.raises(jsf.Halt, match="pagination_cycle"):
        a.work(Client(), "masters", {"id": "securities"})


def test_prepare_freezes_pinned_same_day_liquidity_without_survivorship(tmp_path, monkeypatch):
    history = tmp_path / "data/jquants/history"
    history.mkdir(parents=True)
    with duckdb.connect() as db:
        db.execute("CREATE TABLE b(Date VARCHAR, Code VARCHAR, Va BIGINT)")
        db.execute(
            """INSERT INTO b VALUES ('2023-01-04','10010',500000000),
            ('2023-01-04','10020',499999999),('2023-01-04','10030',900000000),
            ('2021-01-04','10040',900000000),('2023-01-04','10050',900000000)"""
        )
        db.execute("CREATE TABLE m(Date VARCHAR, Code VARCHAR, Mkt VARCHAR, ProdCat VARCHAR)")
        db.execute(
            """INSERT INTO m VALUES ('2023-01-04','10010','0111','011'),
            ('2023-01-04','10020','0112','011'),('2023-01-04','10030','0113','011'),
            ('2021-01-04','10040','0101','011'),('2023-01-05','10050','0112','011')"""
        )
        db.sql("SELECT * FROM b").write_parquet(str(history / "bars.parquet"))
        db.sql("SELECT * FROM m").write_parquet(str(history / "master.parquet"))
    pins = {str(p.relative_to(tmp_path)): m.sha(p) for p in history.glob("*.parquet")}
    quality = {
        "checks": {"missing": 0},
        "input_manifest": [
            {
                "endpoint": endpoint,
                "state": "complete",
                "parquet_path": name,
                "parquet_sha256": m.sha(history / name),
            }
            for endpoint, name in (
                ("/equities/bars/daily", "bars.parquet"),
                ("/equities/master", "master.parquet"),
            )
        ],
    }
    jsf.save(history / "quality-20261031.json", quality)
    universe = {
        "codes": ["1001", "1002", "1003", "1004", "1005"],
        "boundary_only_codes": ["9999"],
        "source_manifest_sha256": m.sha(history / "quality-20261031.json"),
        "source_sha256": pins,
        "start": "2016-11-20",
        "end": "2023-10-31",
    }
    root = tmp_path / "irbank"
    jsf.save(root / "universe.json", universe)
    monkeypatch.setattr(m, "prepare_universe", lambda *_: universe)
    first = m.prepare(tmp_path, root)
    assert [job["code"] for job in first["jobs"]["weekly"]] == [
        "1001",
        "1002",
        "1003",
        "1004",
        "1005",
        "9999",
    ]
    assert first["jobs"]["financials"] == [{"id": "1001", "code": "1001"}]
    assert first["source_sha256"] == pins
    assert m.prepare(tmp_path, root) == first


def test_prepare_source_drift_stops(tmp_path):
    history = tmp_path / "data/jquants/history"
    history.mkdir(parents=True)
    (history / "quality-20261031.json").write_text("{}")
    with pytest.raises(jsf.Halt, match="original_manifest_changed"):
        m._liquid_codes(tmp_path, {"source_manifest_sha256": "wrong"})


def test_lazy_shared_client_preserves_request_limit_across_master_pages(tmp_path, monkeypatch):
    fixture_manifest(tmp_path)

    class PaginatedClient:
        instances = []

        def __init__(self, root, key, max_requests):
            self.sent, self.limit, self.closed = 0, max_requests, False
            self.instances.append(self)

        def get_resource(self, endpoint, params, **kwargs):
            if self.sent == self.limit:
                raise jsf.Halt("run_request_limit")
            self.sent += 1
            return response(
                endpoint,
                cursor="second" if endpoint == "securities" and "cursor" not in params else None,
            )

        def close(self):
            self.closed = True

    monkeypatch.setattr(m, "Client", PaginatedClient)
    monkeypatch.setattr(m, "load_key", lambda _: "synthetic")
    first = m.run(tmp_path, tmp_path, max_requests=1, phase="masters")
    assert first["state"] == "stopped" and first["requests_this_run"] == 1
    assert len(PaginatedClient.instances) == 1 and PaginatedClient.instances[0].closed
    second = m.run(tmp_path, tmp_path, max_requests=2, phase="masters")
    assert second["state"] == "complete" and second["requests_this_run"] == 2
    assert len(PaginatedClient.instances) == 2 and all(c.closed for c in PaginatedClient.instances)


def test_identical_edinet_shareholder_requests_deduplicated_across_codes(tmp_path, fake):
    fixture_manifest(tmp_path, codes=("7203", "9999"), liquid=("7203", "9999"))
    assert m.run(tmp_path, tmp_path, phase="financials")["requests_this_run"] == 2
    report = m.run(tmp_path, tmp_path, phase="shareholders")
    assert report["state"] == "complete" and report["complete"] == 2
    assert report["requests_this_run"] == 1
    assert len(list((tmp_path / "supplement/raw/shareholders").glob("*/*/receipt.json"))) == 1
    states = [
        json.loads((tmp_path / f"supplement/state/shareholders/{code}.json").read_bytes())
        for code in ("7203", "9999")
    ]
    assert [s["code"] for s in states] == ["7203", "9999"]
    assert all(s["cache_job_id"] == "E02144" for s in states)
    assert states[0]["pages"] == states[1]["pages"]


@pytest.mark.parametrize("pin", [None, "0" * 64])
def test_enabled_acquisition_requires_authorized_manifest_pin(tmp_path, pin):
    fixture_manifest(tmp_path)
    jsf.save(
        tmp_path / "acquisition-policy.json",
        {"supplementary_acquisition": {"enabled": True, "manifest_sha256": pin}},
    )
    with pytest.raises(jsf.Halt, match="supplement_authorized_manifest_mismatch"):
        m.read_manifest(tmp_path)
    jsf.save(
        tmp_path / "acquisition-policy.json",
        {
            "supplementary_acquisition": {
                "enabled": True,
                "manifest_sha256": m.sha(tmp_path / "supplement/manifest.json"),
            }
        },
    )
    assert m.read_manifest(tmp_path)["mode"] == m.MODE
