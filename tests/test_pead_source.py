"""Offline contracts for pinned PEAD loading and independent execution evidence."""

import hashlib
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trading_system.research import pead_source

JST = ZoneInfo("Asia/Tokyo")


def test_source_hash_check_precedes_import_and_rechecks_cache(tmp_path, monkeypatch):
    package = tmp_path / "plan_c_exp"
    package.mkdir()
    body = "value = 4\n"
    path = package / "daily_ops.py"
    path.write_bytes(body.replace("\n", "\r\n").encode())
    monkeypatch.setattr(
        pead_source, "SOURCE_SHA256", {"daily_ops": hashlib.sha256(body.encode()).hexdigest()}
    )
    assert pead_source._verified_source(tmp_path) == {"daily_ops": body}
    path.write_text("value = 5\n")
    with pytest.raises(ValueError, match="pinned source mismatch"):
        pead_source._verified_source(tmp_path)


def test_loader_never_uses_an_existing_unpinned_cwd_module(tmp_path, monkeypatch):
    bodies = {"daily_ops": "value = 41\n", "daily_feed": "value = 42\n"}
    folder = tmp_path / "plan_c_exp"
    folder.mkdir()
    for name, body in bodies.items():
        (folder / f"{name}.py").write_text(body)
    monkeypatch.setattr(
        pead_source,
        "SOURCE_SHA256",
        {n: hashlib.sha256(b.encode()).hexdigest() for n, b in bodies.items()},
    )
    monkeypatch.setattr(pead_source, "source_root", lambda: tmp_path)
    monkeypatch.setattr(pead_source, "_bind_feed", lambda feed: None)
    monkeypatch.setattr(pead_source, "_MODULES", {})
    poison = ModuleType("plan_c_exp.daily_ops")
    poison.value = "unverified"
    monkeypatch.setitem(sys.modules, "plan_c_exp.daily_ops", poison)
    assert pead_source.load_pead_module("daily_ops").value == 41
    assert sys.modules["plan_c_exp.daily_ops"] is poison
    assert not list(tmp_path.rglob("__pycache__"))
    (folder / "daily_ops.py").write_text("value = 43\n")
    with pytest.raises(ValueError, match="pinned source mismatch"):
        pead_source.load_pead_module("daily_ops")


def test_loader_rejects_unsupported_module_and_onedrive_path():
    with pytest.raises(ValueError, match="daily_ops or daily_feed"):
        pead_source.load_pead_module("backtest")
    with pytest.raises(ValueError, match="OneDrive"):
        pead_source._local(Path("C:/Users/test/OneDrive/PEAD"))


def _feed(tmp_path, status="bulk_records", refresh=None):
    folder = tmp_path / "source"
    (folder / "data").mkdir(parents=True)
    (folder / "data/share_exchanges.json").write_text("[]")
    sessions = pd.bdate_range("2026-10-01", "2026-12-03")
    feed = SimpleNamespace(
        __file__=str(folder / "daily_feed.py"),
        JST=JST,
        pd=pd,
        np=np,
        HALT=pd.Timestamp("2020-10-01"),
        _stamp=lambda value: pd.Timestamp(value).date().isoformat(),
        _calendar=lambda v1, clock: sessions,
        _num=lambda value: float(value) if np.isfinite(value) else None,
    )

    def original(v1, date, now=None):
        return {
            "date": date,
            "candidates": ["12340"],
            "alerts": [],
            "audit": {"financial_coverage": {date: {"status": status}}},
        }, sessions.strftime("%Y-%m-%d").tolist()

    feed.load_daily_snapshot = original
    feed.refresh_daily_cache = refresh or (lambda v1, day: {"state": "complete"})
    pead_source._bind_feed(feed)
    return feed, sessions


@pytest.mark.parametrize(
    "status,complete",
    [
        ("bulk_records", True),
        ("confirmed_empty_monthly", True),
        ("empty_receipt_unverified", False),
        ("receipt_records_only", False),
        ("receipt_has_extra_records", False),
        ("no_source", False),
    ],
)
def test_incomplete_disclosures_never_become_final_buy_candidates(tmp_path, status, complete):
    feed, _ = _feed(tmp_path, status)
    value, _ = feed.load_daily_snapshot(
        tmp_path, "2026-11-25", now=datetime(2026, 11, 25, 22, tzinfo=JST)
    )
    assert value["audit"]["financial_complete"] is complete
    assert value["candidates"] == (["12340"] if complete else [])
    assert "source_commit" in value["audit"]


def test_execution_uses_received_clock_without_original_signal_deadline(tmp_path):
    feed, sessions = _feed(tmp_path)
    late = datetime(2026, 11, 27, 22, tzinfo=JST)
    seen = []

    def frames(roots, axis, cutoff):
        seen.append((axis.tolist(), cutoff))
        return {
            "master": pd.DataFrame(
                {"Date": ["2026-11-25"], "Code": ["12340"], "CoName": ["sample"], "S33": ["10"]}
            )
        }

    feed._frames = frames
    feed._bulk = lambda *args: ([pd.DataFrame({"Code": ["12340"], "Va": [10000]})], set())
    feed._market = lambda frames, axis: SimpleNamespace(
        codes=["12340"],
        topix_close=np.ones(35),
        fields={
            **{
                name: np.array([[100.0]])
                for name in ("open", "high", "low", "close", "volume", "sma20", "atr")
            },
            "factor": np.array([[0.5]]),
            "share_factor": np.array([[0.5]]),
            "spinoff": np.array([[False]]),
        },
    )
    value, days = feed.load_execution_snapshot(tmp_path, "2026-11-25", now=late)
    assert seen[0][0][-1] == pd.Timestamp("2026-11-25")
    assert len(seen[0][0]) == 35
    assert seen[0][1] == late
    assert value["bars"]["12340"]["factor"] == 0.5
    assert value["bars"]["12340"]["open"] == 100
    assert value["audit"]["purpose"] == "objective_execution_only"
    assert days == sessions.strftime("%Y-%m-%d").tolist()
    with pytest.raises(ValueError, match="not closed"):
        feed.load_execution_snapshot(
            tmp_path, "2026-11-27", now=datetime(2026, 11, 27, 9, tzinfo=JST)
        )
    with pytest.raises(ValueError, match="not closed"):
        feed.load_execution_snapshot(tmp_path, "2026-11-30", now=late)


def test_calendar_requires_future_settlement_and_timezone(tmp_path):
    feed, _ = _feed(tmp_path)
    clock = datetime(2026, 11, 25, 22, tzinfo=JST)
    assert "2026-11-30" in feed.load_trading_days(tmp_path, "2026-11-25", now=clock)
    with pytest.raises(ValueError, match="T\\+2"):
        feed.load_trading_days(tmp_path, "2026-12-03", now=clock)
    with pytest.raises(ValueError, match="timezone"):
        feed.load_daily_snapshot(tmp_path, "2026-11-25", now=datetime(2026, 11, 25))


def test_refresh_imports_validate_current_v1_without_sys_path_mutation():
    original = list(sys.path)
    with pead_source._read_only_imports(["some-other-checkout/src"]):
        assert sys.path == original


def test_cache_refresh_is_shared_throttled_and_never_repairs_replay_clock(tmp_path, monkeypatch):
    clock = datetime(2026, 11, 25, 22, tzinfo=JST)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    calls = []

    def refresh(project, day):
        calls.append(day)
        return {"state": "complete", "date": day}

    feed, _ = _feed(tmp_path, refresh=refresh)
    feed.LOCAL = tmp_path / "cache"
    monkeypatch.setattr(pead_source, "datetime", FixedClock)
    assert feed.ensure_daily_cache(tmp_path, "2026-11-25", now=clock)["state"] == "complete"
    assert feed.ensure_daily_cache(tmp_path, "2026-11-25", now=clock)["state"] == "complete"
    assert calls == ["2026-11-25"]
    assert (
        feed.ensure_daily_cache(tmp_path, "2026-11-24", now=clock)["state"]
        == "historical_session_no_refresh"
    )
    assert (
        feed.ensure_daily_cache(tmp_path, "2026-11-25", now=clock.replace(day=24))["state"]
        == "historical_clock_no_refresh"
    )
    assert calls == ["2026-11-25"]


def test_failed_refresh_retries_after_cooldown_and_does_not_certify_data(tmp_path, monkeypatch):
    clock = datetime(2026, 11, 25, 22, tzinfo=JST)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    calls = []

    def refresh(project, day):
        calls.append(day)
        raise RuntimeError("transport failed")

    feed, _ = _feed(tmp_path, refresh=refresh)
    feed.LOCAL = tmp_path / "cache"
    monkeypatch.setattr(pead_source, "datetime", FixedClock)
    with pytest.raises(ValueError, match="refresh failed: RuntimeError"):
        feed.ensure_daily_cache(tmp_path, "2026-11-25", now=clock)
    with pytest.raises(ValueError, match="cooling down"):
        feed.ensure_daily_cache(tmp_path, "2026-11-25", now=clock)
    clock = clock.replace(minute=6)
    with pytest.raises(ValueError, match="refresh failed: RuntimeError"):
        feed.ensure_daily_cache(tmp_path, "2026-11-25", now=clock)
    assert calls == ["2026-11-25", "2026-11-25"]


@pytest.mark.parametrize(
    "prices,volume",
    [([None, 100, 90, 95], 1), ([0, 0, 0, 0], 0), ([100, 90, 110, 100], 1), ([100] * 4, None)],
)
def test_execution_rejects_missing_or_invalid_bars(prices, volume):
    bar = dict(zip(("open", "high", "low", "close"), prices))
    bar.update(volume=volume, no_trade=False)
    with pytest.raises(ValueError, match="execution"):
        pead_source._validate_execution_bar(bar, "12340")


def test_partial_financial_refresh_remains_retryable(tmp_path, monkeypatch):
    clock = datetime(2026, 11, 25, 22, tzinfo=JST)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    calls = []

    def refresh(project, day):
        calls.append(day)
        return {"state": "complete", "date": day}

    feed, _ = _feed(tmp_path, status="empty_receipt_unverified", refresh=refresh)
    feed.LOCAL = tmp_path / "cache"
    monkeypatch.setattr(pead_source, "datetime", FixedClock)
    assert feed.ensure_daily_cache(tmp_path, "2026-11-25", now=clock)["state"] == "partial"
    assert feed.ensure_daily_cache(tmp_path, "2026-11-25", now=clock)["state"] == "partial"
    assert calls == ["2026-11-25"]
    clock = clock.replace(minute=6)
    assert feed.ensure_daily_cache(tmp_path, "2026-11-25", now=clock)["state"] == "partial"
    assert calls == ["2026-11-25", "2026-11-25"]


@pytest.mark.parametrize("volume,turnover", [(None, None), (0, None), (None, 0), (0, 0)])
def test_execution_no_trade_requires_all_six_ohlcv_and_turnover_values_null(
    tmp_path, volume, turnover
):
    feed, _ = _feed(tmp_path)
    feed._frames = lambda *args: {
        "master": pd.DataFrame(
            {"Date": ["2026-11-25"], "Code": ["12340"], "CoName": ["sample"], "S33": ["10"]}
        )
    }
    feed._bulk = lambda *args: ([pd.DataFrame({"Code": ["12340"], "Va": [turnover]})], set())
    feed._market = lambda frames, axis: SimpleNamespace(
        codes=["12340"],
        topix_close=np.ones(35),
        fields={
            **{
                name: np.array([[np.nan]])
                for name in ("open", "high", "low", "close", "sma20", "atr")
            },
            "volume": np.array([[np.nan if volume is None else volume]]),
            "factor": np.array([[1.0]]),
            "share_factor": np.array([[1.0]]),
            "spinoff": np.array([[False]]),
        },
    )
    clock = datetime(2026, 11, 27, 22, tzinfo=JST)
    if volume is None and turnover is None:
        value, _ = feed.load_execution_snapshot(tmp_path, "2026-11-25", now=clock)
        assert value["bars"]["12340"]["no_trade"] is True
    else:
        with pytest.raises(ValueError, match="OHLCV is incomplete or invalid"):
            feed.load_execution_snapshot(tmp_path, "2026-11-25", now=clock)
