import gzip
import json
import sqlite3
from datetime import timedelta

import httpx
import pytest
from test_research_panel import CODES, T

from trading_system import kabu_study
from trading_system.brokers.mufg_esmart.connection_check import BASE, ProbeError, ReadOnlyTransport
from trading_system.brokers.mufg_esmart.market_data import Client


def test_panel_transport_has_no_order_account_or_arbitrary_symbol_access():
    seen = []

    def backend(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={})

    with httpx.Client(
        transport=ReadOnlyTransport(httpx.MockTransport(backend), board_symbols=("5976", "7203"))
    ) as c:
        for url in (
            BASE + "/sendorder",
            BASE + "/positions",
            BASE + "/board/1319@1",
            BASE + "/board/5976@1?extra=1",
            "http://example.org/kabusapi/board/5976@1",
        ):
            with pytest.raises(ProbeError, match="not_allowed"):
                c.get(url)
        c.get(BASE + "/board/5976@1")
    assert seen == [BASE + "/board/5976@1"]


def test_one_pass_records_31_evening_boards_shared_token_and_retries_once(tmp_path):
    folder = tmp_path / "data/rss-panel"
    folder.mkdir(parents=True)
    (folder / "panel.json").write_text(json.dumps(dict(version=1, symbols=CODES)))
    now = T.replace(hour=20)
    calls = []

    def backend(request):
        if request.url.path.endswith("/token"):
            calls.append("token")
            return httpx.Response(200, json={"ResultCode": 0, "Token": "stub-token-alone"})
        symbol = request.url.path.split("/")[-1].split("@")[0]
        calls.append(symbol)
        if symbol == "7203" and calls.count(symbol) == 1:
            raise httpx.ConnectTimeout("fixture")
        return httpx.Response(
            200,
            json={
                "Symbol": symbol,
                "Exchange": 1,
                "CurrentPrice": 100,
                "TradingVolume": 400,
                "CurrentPriceTime": T.isoformat(),
                "Sell1": {"Price": 101, "Qty": 200},
                "Buy1": {"Price": 99, "Qty": 100},
            },
        )

    def factory(password, **kwargs):
        return Client(password, transport_factory=lambda: httpx.MockTransport(backend), **kwargs)

    kabu_study.run(
        tmp_path,
        int(now.timestamp() * 1e9),
        "none",
        one_pass=True,
        clock=lambda: now,
        sleep=lambda _: None,
        prompt=lambda *_, **kw: "password1",
        client_factory=factory,
        listener_ready=lambda: True,
    )
    assert calls.count("token") == 1 and calls.count("7203") == 2
    assert set(calls) - {"token"} == {s[:-2] for s in CODES}
    root = tmp_path / "data/mufg-panel"
    with sqlite3.connect(root / "research.sqlite") as db:
        assert db.execute("SELECT count(*) FROM levels").fetchone() == (62,)
        assert db.execute(
            "SELECT raw_board_gzip FROM samples WHERE operation='token'"
        ).fetchone() == (None,)
        for (blob,) in db.execute(
            "SELECT raw_board_gzip FROM samples WHERE raw_board_gzip IS NOT NULL"
        ):
            assert b"stub-token-alone" not in gzip.decompress(blob)
    assert json.loads((root / "status.json").read_bytes())["symbols_with_book"] == 31


def test_panel_password_wait_observes_original_close(tmp_path):
    cfg = tmp_path / "data/rss-panel"
    cfg.mkdir(parents=True)
    (cfg / "panel.json").write_text(json.dumps({"version": 1, "symbols": CODES}))
    clock = [T.replace(hour=15, minute=58)]

    def prompt(stop, *args, **kwargs):
        clock[0] += timedelta(minutes=2)
        assert stop() == "daytime_1600"
        return "not-used"

    def forbidden(*a, **kw):
        pytest.fail("network client constructed after cutoff")

    kabu_study.run(
        tmp_path,
        int(clock[0].timestamp() * 1e9),
        "none",
        clock=lambda: clock[0],
        prompt=prompt,
        client_factory=forbidden,
        listener_ready=lambda: True,
    )
    data = json.loads((tmp_path / "data/mufg-panel/status.json").read_bytes())
    assert data["reason"] == "daytime_1600"


def make_panel(project):
    panel = project / "data/rss-panel"
    panel.mkdir(parents=True)
    (panel / "panel.json").write_text(json.dumps({"version": 1, "symbols": CODES}))
    return project / "data/mufg-panel"


@pytest.mark.parametrize(
    "ending", ["restart", "manual_stop", "daytime_1600", "station_wait_timeout"]
)
def test_station_wait_keeps_credentials_and_board_calls_until_ready(tmp_path, monkeypatch, ending):
    root = make_panel(tmp_path)
    now = [T.replace(hour=15, minute=58)]
    elapsed = [0.0]
    calls = []
    ready_after = 320  # Simulate a login taking longer than the old failure budget.
    monkeypatch.setattr(kabu_study.time, "monotonic", lambda: elapsed[0])

    def sleep(seconds):
        elapsed[0] += seconds
        if ending == "manual_stop":
            (root / "STOP").touch()
        if ending == "daytime_1600":
            now[0] = now[0].replace(hour=16)

    def prompt(*args, **kwargs):
        assert ending == "restart" and elapsed[0] >= ready_after
        calls.append("prompt")
        return "password1"

    def backend(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"ResultCode": 0, "Token": "test-token"})
        (root / "STOP").touch()
        return httpx.Response(
            200,
            json={
                "Symbol": "7203",
                "Exchange": 1,
                "Buy1": {"Price": 100, "Qty": 100},
                "Sell1": {"Price": 101, "Qty": 100},
            },
        )

    def factory(password, **kwargs):
        return Client(password, transport_factory=lambda: httpx.MockTransport(backend), **kwargs)

    def is_ready():
        if elapsed[0]:
            assert json.loads((root / "status.json").read_bytes())["state"] == "waiting_for_station"
        return elapsed[0] >= ready_after

    kabu_study.run(
        tmp_path,
        int(now[0].timestamp() * 1e9),
        "none",
        one_pass=ending == "station_wait_timeout",
        clock=lambda: now[0],
        sleep=sleep,
        prompt=prompt,
        client_factory=factory,
        listener_ready=is_ready,
    )
    state = json.loads((root / "status.json").read_bytes())
    if ending == "restart":
        assert calls == ["prompt", "/kabusapi/token", "/kabusapi/board/7203@1"]
        assert state["symbols_with_book"] == 1
    else:
        assert state["reason"] == ending
        assert not calls and not (root / "research.sqlite").exists()


def test_retry_waits_for_station_after_network_loss_and_keeps_failed_sample(tmp_path):
    root = make_panel(tmp_path)
    state = {"board_calls": 0, "down": False, "elapsed": 0}

    def backend(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"ResultCode": 0, "Token": "fixture"})
        state["board_calls"] += 1
        if state["board_calls"] == 1:
            state["down"] = True
            raise httpx.ConnectError("fixture")
        assert not state["down"]
        (root / "STOP").touch()
        return httpx.Response(
            200,
            json={
                "Symbol": req.url.path.rsplit("/", 1)[-1].split("@")[0],
                "Exchange": 1,
                "Buy1": {"Price": 100, "Qty": 100},
                "Sell1": {"Price": 101, "Qty": 100},
            },
        )

    def advance(seconds):
        if state["down"]:
            assert json.loads((root / "status.json").read_bytes())["state"] == "waiting_for_station"
            state["elapsed"] += seconds
            if state["elapsed"] >= 310:
                state["down"] = False

    kabu_study.run(
        tmp_path,
        int(T.timestamp() * 1e9),
        "none",
        clock=lambda: T,
        sleep=advance,
        prompt=lambda *args, **kw: "password1",
        listener_ready=lambda: not state["down"],
        client_factory=lambda p, **kw: Client(
            p, transport_factory=lambda: httpx.MockTransport(backend), **kw
        ),
    )
    with sqlite3.connect(root / "research.sqlite") as db:
        assert db.execute("SELECT operation,quality FROM samples ORDER BY id").fetchall() == [
            ("token", "ok"),
            ("board", "network_request_failed"),
            ("board", "observed_book"),
        ]
