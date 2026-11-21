import json
from datetime import datetime

import httpx
import pytest

from trading_system.research import jsf_daily_compare as m


def body(loan=100, restriction="注意"):
    return (
        "銘柄コード,市場区分,制限措置等,融資残高（株）,貸株残高（株）,差引残高（株）,品貸料率（円）\n"
        f"7203,東証,{restriction},{loan},10,{loan - 10},0.05\n"
        "7203,名証,,20,5,15,-\n"
    ).encode("cp932")


def remote(monkeypatch, day="2026 / 11 / 20", csv=None):
    monkeypatch.setattr(
        m.swing_credit,
        "fetch",
        lambda *a: httpx.Response(200, content=f'<input name="mkYmd" value="{day}">'.encode()),
    )
    calls = []

    def query(client, date, detail):
        calls.append(date)
        return b"official form", csv or body(), "https://www.taisyaku.jp/app/stock/csv"

    monkeypatch.setattr(m.swing_credit, "query", query)
    return calls


def test_all_markets_and_original_restrictions_remain_separate():
    x = m.normalize(body(), "2026-11-20")
    assert x["all_market_balances"]["72030"]["差引残高（株）"] == "105"
    assert x["all_market_balances"]["72030"]["制限措置等"] == ""
    assert x["market_rows"][0]["制限措置等"] == "注意"
    assert x["market_rows"][0]["品貸料率（円）"] == "0.05"
    assert not x["historical_publication_verified"]


def test_unknown_component_never_becomes_zero():
    raw = body().replace(b"20,5,15", b",5,")
    x = m.normalize(raw, "2026-11-20")
    assert x["all_market_balances"] == {}
    assert x["quality"]["unobserved_codes"] == ["72030"]


def test_revisions_are_immutable_and_publication_time_unknown(tmp_path, monkeypatch):
    calls = remote(monkeypatch)
    a = m.collect(tmp_path, None, run="a")
    path = tmp_path / next(p for p in a["outputs"] if p.endswith(".csv"))
    original = path.read_bytes()
    remote(monkeypatch, csv=body(200))
    b = m.collect(tmp_path, None, run="b")
    assert calls == ["2026-11-20"]
    assert a["state"] == b["state"] == "checked"
    assert path.read_bytes() == original
    assert a["outputs"] != b["outputs"]
    assert b["sources"][0]["source_published_at"] is None
    assert b["sources"][0]["available_at"] > b["sources"][0]["observed_at"]
    path.write_bytes(b"tamper")
    with pytest.raises(ValueError, match="archive_changed"):
        m.collect(tmp_path, None, run="c")


def test_refresh_is_bounded_and_failed_parse_is_not_complete(tmp_path, monkeypatch):
    for day in range(10, 17):
        calls = remote(monkeypatch, f"2026 / 11 / {day}")
        result = m.collect(tmp_path, None, run=str(day))
        assert len(calls) <= m.MAX_DATES
    assert len(result["dates"]) == 5
    remote(monkeypatch, csv=b"wrong,header\n1,2\n")
    bad = m.collect(tmp_path, None, run="bad")
    assert bad["state"] == "issues_found"
    assert any(p.endswith(".csv") for p in bad["outputs"])
    remote(monkeypatch, day="2026 / 11 / 16")
    assert m.collect(tmp_path, None, run="recovery")["state"] == "checked"


def test_stop_and_future_date(tmp_path, monkeypatch):
    remote(monkeypatch, day="2026 / 11 / 20")
    (tmp_path / "STOP").touch()
    assert m.collect(tmp_path, None)["state"] == "stopped"
    (tmp_path / "STOP").unlink()
    with pytest.raises(ValueError, match="future_application"):
        m.collect(tmp_path, None, moment=datetime.fromisoformat("2026-11-19T08:00:00+09:00"))


def test_old_generic_success_does_not_satisfy_new_collection(tmp_path):
    from trading_system.free_market import schedule_decision

    (tmp_path / "report-old.json").write_text(
        json.dumps(
            {
                "run": "20261120T000000-old",
                "checked_at": "2026-11-20T00:30:00+00:00",
                "state": "checked",
                "issues": [],
            }
        )
    )
    assert (
        schedule_decision(tmp_path, datetime.fromisoformat("2026-11-20T12:00:00+09:00"))["state"]
        == "due"
    )


@pytest.mark.parametrize("case", ["success", "partial", "throttled"])
def test_morning_entrypoint_includes_new_collection_and_failure_state(tmp_path, monkeypatch, case):
    from types import SimpleNamespace

    from trading_system import free_market

    generic = {"run": "20261120T000000-fixture", "state": "checked", "issues": []}
    if case == "throttled":
        generic.update(state="issues_found", issues=[{"http_status": 429}])
    monkeypatch.setattr(free_market.PublicArchive, "collect", lambda *a: generic)
    calls = []

    def collect(*args, **kwargs):
        calls.append(kwargs["run"])
        return {"schema": m.SCHEMA, "state": "checked" if case == "success" else "issues_found"}

    monkeypatch.setattr(m, "collect", collect)
    code = free_market.run_public(SimpleNamespace(root=tmp_path, due=False))
    saved = json.loads((tmp_path / "status.json").read_bytes())
    assert code == (0 if case == "success" else 1)
    assert saved["jsf_comparison"]["schema"] == m.SCHEMA
    assert calls == ([] if case == "throttled" else [generic["run"]])
