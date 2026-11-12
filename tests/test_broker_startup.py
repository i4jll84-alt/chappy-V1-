"""Exercise the actual Tk action and both recorder loops with isolated vendor inputs."""

import sqlite3
import time
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from tkinter import ttk
from unittest.mock import Mock

import httpx
import pytest
from test_research_panel import T, sample
from test_research_workbook import fixture

from trading_system import desktop_launcher as launcher
from trading_system import kabu_study, research_panel
from trading_system.brokers.mufg_esmart.market_data import Client
from trading_system.research_status import active_status
from trading_system.tk_startup import create_window


@pytest.mark.parametrize("waiting", ["rakuten", "mufg_esmart"])
def test_one_click_collects_while_other_login_pending(tmp_path, monkeypatch, waiting):
    root = tmp_path / "data/rss-panel"
    root.parent.mkdir()
    fixture(root)
    stamp = int(T.timestamp() * 1e9)
    monkeypatch.setattr(launcher, "time_ns", lambda: stamp)
    monkeypatch.setattr(launcher, "ensure_broker_app", Mock())
    unlocked, entered = Event(), Event()
    starts = []
    children = []
    pool = ThreadPoolExecutor(max_workers=2)
    opts = dict(clock=lambda: T, sleep=lambda _: time.sleep(0.003))

    def rss(*args):
        payload = sample()
        if waiting == "rakuten" and not unlocked.is_set():
            for item in payload["items"]:
                item.update(chart_status="connection_required", bars=[], quote=[None] * 10)
        return payload

    def backend(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"ResultCode": 0, "Token": "test-token"})
        code = req.url.path.split("/")[-1].split("@")[0]
        return httpx.Response(
            200,
            json={
                "Symbol": code,
                "Exchange": 1,
                "CurrentPrice": 100,
                "CurrentPriceTime": T.isoformat(),
                "Buy1": {"Price": 99, "Qty": 100},
                "Sell1": {"Price": 101, "Qty": 100},
            },
        )

    def factory(password, **kw):
        return Client(password, transport_factory=lambda: httpx.MockTransport(backend), **kw)

    def prompt(stop, *_, **kw):
        while waiting == "mufg_esmart" and not entered.wait(0.01):
            if stop():
                return None
        return "fixture1"

    def start(args, **kwargs):
        assert args[args.index("--requested") + 1] == str(stamp)
        assert kwargs["cwd"] == tmp_path
        module = args[args.index("-m") + 1]
        starts.append(module)
        if module == "trading_system.research_panel":
            child = pool.submit(
                research_panel.run,
                root,
                stamp,
                "none",
                **opts,
                opener=lambda _: None,
                reader=rss,
            )
        else:
            assert module == "trading_system.kabu_study"
            child = pool.submit(
                kabu_study.run,
                tmp_path,
                stamp,
                "none",
                **opts,
                prompt=prompt,
                client_factory=factory,
                listener_ready=lambda: waiting != "mufg_esmart" or unlocked.is_set(),
            )
        children.append(child)
        return Mock(poll=lambda: 0 if child.done() else None)

    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    window = create_window(tk)
    window.withdraw()
    view = launcher.Launcher(window, tmp_path)
    widgets = [window]
    for w in widgets:
        widgets.extend(w.winfo_children())
    labels = {str(w.cget("textvariable")): w for w in widgets if isinstance(w, ttk.Label)}

    def displayed(broker):
        return window.getvar(labels[str(view.status[broker])].cget("textvariable"))

    def until(ready):
        limit = time.monotonic() + 12
        while time.monotonic() < limit:
            view.refresh()
            if ready():
                return
            for future in children:
                if future.done():
                    future.result()
            time.sleep(0.02)
        pytest.fail(f"{displayed('panel')} / {displayed('kabu_panel')}")

    try:
        next(
            w for w in widgets if isinstance(w, ttk.Radiobutton) and w.cget("text") == "両方"
        ).invoke()
        next(
            w for w in widgets if isinstance(w, ttk.Button) and "研究取得" in w.cget("text")
        ).invoke()
        if waiting == "rakuten":
            until(
                lambda: "未接続" in displayed("panel") and "板を巡回中" in displayed("kabu_panel")
            )
            with sqlite3.connect(tmp_path / "data/mufg-panel/research.sqlite") as db:
                assert (
                    db.execute(
                        "select count(*) from samples where quality='observed_book'"
                    ).fetchone()[0]
                    > 0
                )
            unlocked.set()
        else:
            until(
                lambda: (
                    "当日分足受信中 31/31" in displayed("panel")
                    and "ログイン" in displayed("kabu_panel")
                )
            )
            with sqlite3.connect(root / "research.sqlite") as db:
                assert db.execute("select count(distinct symbol) from bars").fetchone()[0] == 31
            unlocked.set()
            until(
                lambda: (
                    "専用画面" in displayed("kabu_panel")
                    and "APIパスワード" in displayed("kabu_panel")
                )
            )
            entered.set()
        until(
            lambda: (
                "当日分足受信中 31/31" in displayed("panel")
                and "板を巡回中" in displayed("kabu_panel")
            )
        )
        assert len(starts) == len(set(starts)) == 2
        assert {c.args[1] for c in launcher.ensure_broker_app.call_args_list} == set(
            launcher.BROKERS
        )
    finally:
        for broker in launcher.RESEARCH.values():
            launcher.request_stop(tmp_path, broker)
        entered.set()
        unlocked.set()
        view.vendor_executor.shutdown()
        pool.shutdown()
        view.daily_executor.shutdown()
        window.destroy()
    for child in children:
        child.result()


def test_pending_vendor_does_not_block_other_or_restart_after_stop(tmp_path, monkeypatch):
    from test_desktop_launcher import research_view

    view = research_view(tmp_path, "both")
    pending, ready = Future(), Future()
    ready.set_result(None)
    view.vendor_executor.submit.side_effect = [pending, ready, ready]
    spawn = Mock()
    spawn.return_value.poll.return_value = None
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher, "time_ns", lambda: int(T.timestamp() * 1e9))
    view.start_research()
    assert set(view.children) == {"kabu_panel"}
    view.start_research()
    assert spawn.call_count == 1
    view.stop_all()
    view.refresh_research_starts()
    assert pending.cancelled() and spawn.call_count == 1


@pytest.mark.parametrize(
    "state,fields,at,expected",
    [
        (
            "read",
            {"current_day_chart_symbols": 0, "streaming_charts": 31},
            T.replace(hour=8),
            "寄り付き待ち",
        ),
        ("read", {"current_day_chart_symbols": 0}, T, "当日分足 0/31"),
        (
            "read",
            {"current_day_chart_symbols": 0},
            T.replace(hour=12, minute=0),
            "昼休み",
        ),
        (
            "read",
            {
                "current_day_chart_symbols": 31,
                "latest_chart_at": (T - timedelta(minutes=7)).isoformat(),
            },
            T,
            "更新待ち",
        ),
        ("read", {"current_day_chart_symbols": 31}, T.replace(hour=16), "引け後"),
        (
            "recording",
            {
                "board_reads": 500,
                "current_day_board_symbols": 0,
                "last_board_at": T.isoformat(),
            },
            T,
            "0/31",
        ),
        (
            "recording",
            {"board_reads": 500, "reason": "network_request_failed"},
            T,
            "通信再試行中",
        ),
    ],
)
def test_status_uses_market_phase_and_recorded_evidence(state, fields, at, expected):
    assert expected in active_status(
        "panel" if state == "read" else "kabu_panel",
        dict(state=state, updated_at=at.isoformat(), **fields),
        at,
    )


def test_board_market_time_is_independent_of_request_arrival():
    yesterday = T - timedelta(days=1)
    for value in (
        None,
        yesterday.isoformat(),
        (T + timedelta(seconds=1)).isoformat(),
        T.replace(tzinfo=None).isoformat(),
    ):
        assert not kabu_study.current_market_time(value, T)
    assert kabu_study.current_market_time(T.isoformat(), T)


@pytest.mark.parametrize("source,key", [("rakuten", "rakuten_app"), ("mufg_esmart", "mufg_app")])
@pytest.mark.parametrize("result", [0, 3, 1])
def test_vendor_probe_opens_only_absent_app(tmp_path, monkeypatch, source, key, result):
    path = tmp_path / "test.exe"
    path.touch()
    run = Mock(return_value=Mock(returncode=result))
    opening = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher, "open_vendor", opening)
    if result == 1:
        with pytest.raises(OSError):
            launcher.ensure_broker_app(tmp_path, source, path)
    else:
        launcher.ensure_broker_app(tmp_path, source, path)
    assert run.call_args.args[0][-4:] == ["-AppPath", str(path), "-Broker", source]
    if result == 3:
        opening.assert_called_once_with(key, path)
    else:
        opening.assert_not_called()
