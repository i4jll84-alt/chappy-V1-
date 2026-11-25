"""Hash-pinned bridge to the separately published PEAD study; never imports from cwd.

Clone v1-strategy-lab at PEAD_SOURCE_COMMIT beside V1 (or set PEAD_SOURCE_ROOT).
The ignored share-exchange terms must be supplied and verified locally as well.
All market refreshes write into V1, never into that source checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, time
from pathlib import Path
from threading import RLock
from types import ModuleType

PEAD_SOURCE_REPOSITORY = "https://github.com/i4jll84-alt/v1-strategy-lab.git"
PEAD_SOURCE_COMMIT = "28c271e9dfba9cee18f21159866256add14cfb8f"
PEAD_LOCAL_SOURCE_COMMIT = "95897a822362e997b08fe0165ad9f0ef49b8fa51"
# SHA256 of UTF-8 files with CRLF normalized to LF, covering every imported study module.
SOURCE_SHA256 = {
    "feed": "bf89878379a6c90c858d01f2ce04b3a6a9ea94d3d1a653bd2ffa3f650e170e3e",
    "fundamentals": "0af3935a808fe0cd6b1f30d01bb4dd1c7645fb7a6ca25629ddc2d2045d3d6da0",
    "strategies": "9004268586a4c0794598de0dd4d69cdc174461d7917f89d5b08aedb4b0cdd348",
    "strategies_v2": "f1505626edf62a492e7857747df4cb42de0e40edf3b54b9d824cc16c12ad1098",
    "pead_final_strategy": "84544afd6cbe6ff8e93051314881219063ffde7eb07b406a17b77aef0a9aa95e",
    "daily_ops": "570c8b7a12bfbc9404fdc9ddaf5f1a77aedc9f63a6b5d9848735b45e11840fd0",
    "daily_feed": "b183224950a8d72ea7dfd7490c566847455fc5d451f09c59aea5eb37ee793efd",
}
_MODULES = {}
_LOCK = RLock()
_COMPLETE_FINANCIAL = {"bulk_records", "confirmed_empty_monthly", "no_dated_file_on_published_list"}


def _local(path):
    path = Path(path).expanduser().resolve()
    if any(part.casefold().startswith("onedrive") for part in path.parts):
        raise ValueError("PEAD requires a local path outside OneDrive")
    return path


def source_root():
    project = Path(__file__).resolve().parents[3]
    return _local(
        os.environ.get("PEAD_SOURCE_ROOT", str(project) + "トレード手法検討バックテスト用")
    )


def _verified_source(root):
    source = {}
    for name, digest in SOURCE_SHA256.items():
        path = root / "plan_c_exp" / f"{name}.py"
        body = path.read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError(f"PEAD pinned source mismatch: {path}; use {PEAD_SOURCE_COMMIT}")
        source[name] = body.decode("utf-8")
    return source


@contextmanager
def _read_only_imports(paths):
    # Production trading_system is already loaded from V1. Never add another
    # checkout to sys.path; this helper replaces only the study's import helper.
    expected = Path(__file__).resolve().parents[1]
    import trading_system

    if Path(trading_system.__file__).resolve().parent != expected:
        raise ValueError("PEAD refresh loaded another trading_system checkout")
    yield


def load_pead_module(name):
    """Load only the pinned signal/operator closure in an isolated package namespace."""
    if name not in ("daily_ops", "daily_feed"):
        raise ValueError("PEAD module must be daily_ops or daily_feed")
    root = source_root()
    with _LOCK:
        sources = _verified_source(root)  # Recheck even when module objects are cached.
        if root not in _MODULES:
            namespace = "_v1_pead_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
            package = ModuleType(namespace)
            package.__path__ = []
            sys.modules[namespace] = package
            runtime = ModuleType(namespace + "._bridge_runtime")
            runtime._read_only_imports = _read_only_imports
            sys.modules[runtime.__name__] = runtime
            loaded = {}
            try:
                for module_name, body in sources.items():
                    full_name = namespace + "." + module_name
                    module = ModuleType(full_name)
                    module.__package__ = namespace
                    module.__file__ = str(root / "plan_c_exp" / f"{module_name}.py")
                    sys.modules[full_name] = module
                    if module_name == "daily_feed":
                        body = body.replace(
                            "from strategy_lab.native_a import _read_only_imports",
                            "from ._bridge_runtime import _read_only_imports",
                        )
                    exec(compile(body, module.__file__, "exec"), module.__dict__)
                    loaded[module_name] = module
                _bind_feed(loaded["daily_feed"])
            except BaseException:
                for key in list(sys.modules):
                    if key == namespace or key.startswith(namespace + "."):
                        del sys.modules[key]
                raise
            _MODULES[root] = loaded
        return _MODULES[root][name]


def _clock(feed, now):
    value = now or datetime.now(feed.JST)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("PEAD as-of clock must include timezone")
    return value.astimezone(feed.JST)


def _bind_feed(feed):
    project = Path(__file__).resolve().parents[3]
    feed.LOCAL = _local(project / "data/plan_c_pead_v1/jquants")
    original_snapshot = feed.load_daily_snapshot
    original_refresh = feed.refresh_daily_cache

    def calendar(v1, date=None, *, now=None):
        clock = _clock(feed, now)
        sessions = feed._calendar(_local(v1), clock)
        target = feed._stamp(date or clock)
        ix = sessions.searchsorted(target)
        if ix >= len(sessions) or feed._stamp(sessions[ix]) != target or ix + 3 >= len(sessions):
            raise ValueError("PEAD calendar requires target and next session T+2 settlement")
        return sessions.strftime("%Y-%m-%d").tolist()

    def snapshot(v1, date=None, *, now=None):
        clock = _clock(feed, now)
        value, sessions = original_snapshot(_local(v1), date, now=clock)
        coverage = value["audit"]["financial_coverage"]
        complete = bool(coverage) and all(
            record["status"] in _COMPLETE_FINANCIAL for record in coverage.values()
        )
        value["audit"].update(
            source_commit=PEAD_SOURCE_COMMIT,
            financial_complete=complete,
            source_local_commit=PEAD_LOCAL_SOURCE_COMMIT,
        )
        if not complete:
            # Retain verified prices for existing-position exits. A bullish plan
            # must retry instead of freezing candidates from incomplete disclosures.
            value["candidates"] = []
            value["alerts"].append("決算原本の全日照合が未完了です。新規買い判定を保留します。")
        return value, sessions

    def execution(v1, date, *, now=None):
        """Objective execution bars as actually received by now, separate from signal PIT."""
        v1 = _local(v1)
        clock = _clock(feed, now)
        sessions = feed._calendar(v1, clock)
        day = feed.pd.Timestamp(feed._stamp(date))
        if day not in sessions or day == feed.HALT:
            raise ValueError("PEAD execution date is not a trading session")
        if day.date() > clock.date() or (
            day.date() == clock.date() and clock.time() < time(15, 30)
        ):
            raise ValueError("PEAD execution bar is not closed")
        if sessions.get_loc(day) + 2 >= len(sessions):
            raise ValueError("PEAD execution settlement calendar is incomplete")
        index = sessions.get_loc(day)
        axis = sessions[max(0, index - 34) : index + 1]
        if len(axis) < 26:
            raise ValueError("PEAD execution indicator history is incomplete")
        frames = feed._frames([v1 / "data/jquants", feed.LOCAL], axis, clock)
        market = feed._market(frames, axis)
        names = frames["master"].loc[frames["master"].Date.eq(feed._stamp(day))].set_index("Code")
        terms = json.loads((Path(feed.__file__).parent / "data/share_exchanges.json").read_bytes())
        turnover_parts, _ = feed._bulk(
            [v1 / "data/jquants", feed.LOCAL],
            "bars",
            ["Date", "Code", "Va"],
            feed._stamp(day),
            feed._stamp(day),
            clock,
        )
        if not turnover_parts or not any(not frame.empty for frame in turnover_parts):
            recs = feed._receipts(
                [v1 / "data/jquants", feed.LOCAL], feed._stamp(day), feed._stamp(day), clock
            )
            if feed._stamp(day) not in recs:
                raise ValueError("PEAD execution turnover receipt is missing")
            _, receipt_root, rec = recs[feed._stamp(day)]
            turnover_parts = [feed._read_price_page_frame(receipt_root, rec, clock)[["Code", "Va"]]]
        turnover = (
            feed.pd.concat(turnover_parts).drop_duplicates("Code", keep="last").set_index("Code").Va
        )
        bars = {}
        for col, code in enumerate(market.codes):
            if code not in names.index:
                continue
            listed = names.loc[code]
            if any(
                feed.np.isinf(market.fields[field][-1, col])
                for field in ("open", "high", "low", "close", "volume")
            ):
                raise ValueError(f"PEAD execution contains infinite price/volume: {code}")
            bars[code] = {
                field: feed._num(market.fields[field][-1, col])
                for field in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "sma20",
                    "factor",
                    "share_factor",
                )
            }
            if code not in turnover.index:
                raise ValueError(f"PEAD execution turnover is missing: {code}")
            amount = feed.pd.to_numeric(turnover.loc[code], errors="raise")
            if not feed.pd.isna(amount) and (not feed.np.isfinite(amount) or amount < 0):
                raise ValueError(f"PEAD execution turnover is invalid: {code}")
            bars[code]["turnover"] = None if feed.pd.isna(amount) else float(amount)
            bars[code].update(
                atr14=feed._num(market.fields["atr"][-1, col]),
                no_trade=all(
                    bars[code][field] is None
                    for field in ("open", "high", "low", "close", "volume", "turnover")
                ),
                spinoff=bool(market.fields["spinoff"][-1, col]),
                name=str(listed.CoName),
                sector33=str(listed.S33),
            )
            _validate_execution_bar(bars[code], code)
        return {
            "date": feed._stamp(day),
            "topix_close": float(market.topix_close[-1]),
            "topix_sma20": float(
                feed.pd.Series(market.topix_close, index=axis)[axis != feed.HALT].tail(20).mean()
            ),
            "candidates": [],
            "alerts": [],
            "bars": bars,
            "exchanges": [
                x
                for x in terms
                if x["effective"] == feed._stamp(day) and x["known_on"] <= feed._stamp(day)
            ],
            "audit": {
                "as_of": clock.isoformat(),
                "source_commit": PEAD_SOURCE_COMMIT,
                "purpose": "objective_execution_only",
            },
        }, sessions.strftime("%Y-%m-%d").tolist()

    def ensure_cache(v1, date, *, now=None):
        """Refresh only the latest ended session; throttle shared account retries locally.

        This is called before prospective decisions and objective EOD processing.
        No network call repairs a past missed decision; original signal PIT remains capped.
        """
        from trading_system.brokers.locking import single_writer
        from trading_system.storage.store import atomic_write

        v1 = _local(v1)
        clock = _clock(feed, now)
        # A supplied replay clock never authorizes today's network data as past evidence.
        if abs((datetime.now(feed.JST) - clock).total_seconds()) > 120:
            return {"state": "historical_clock_no_refresh"}
        sessions = feed._calendar(v1, clock)
        today = feed.pd.Timestamp(clock.date())
        index = sessions.searchsorted(today, side="right") - 1
        if index >= 0 and sessions[index] == today and clock.time() < time(16, 30):
            index -= 1
        day = feed._stamp(date)
        if index < 0 or day != feed._stamp(sessions[index]):
            return {"state": "historical_session_no_refresh"}
        status_path = feed.LOCAL / "bridge-refresh" / (day + ".json")
        with single_writer(status_path.parent):
            status = json.loads(status_path.read_bytes()) if status_path.is_file() else {}
            previous = status.get("recorded_at")
            if previous:
                age = (clock - datetime.fromisoformat(previous)).total_seconds()
                if 0 <= age < (3600 if status.get("state") == "complete" else 300):
                    if status.get("state") == "failed":
                        raise ValueError("PEAD cache refresh retry is cooling down")
                    return status
            try:
                result = original_refresh(v1, day)
                checked, _ = original_snapshot(v1, day, now=datetime.now(feed.JST))
                coverage = checked["audit"]["financial_coverage"]
                certified = bool(coverage) and all(
                    record["status"] in _COMPLETE_FINANCIAL for record in coverage.values()
                )
                result = dict(result, state="complete" if certified else "partial")
            except Exception as exc:
                atomic_write(
                    status_path,
                    json.dumps(
                        {
                            "state": "failed",
                            "recorded_at": clock.isoformat(),
                            "error_type": type(exc).__name__,
                        }
                    ).encode(),
                )
                raise ValueError("PEAD cache refresh failed: " + type(exc).__name__) from exc
            status = dict(result, recorded_at=datetime.now(feed.JST).isoformat())
            atomic_write(status_path, json.dumps(status).encode())
            return status

    feed.ensure_daily_cache = ensure_cache
    feed.load_trading_days = calendar
    feed.load_daily_snapshot = snapshot
    feed.load_execution_snapshot = execution


def _validate_execution_bar(bar, code):
    prices = [bar[k] for k in ("open", "high", "low", "close")]
    volume = bar["volume"]
    if bar["no_trade"]:
        if any(
            bar[field] is not None
            for field in ("open", "high", "low", "close", "volume", "turnover")
        ):
            raise ValueError(f"PEAD execution no-trade evidence is ambiguous: {code}")
        return
    if any(value is None or value <= 0 for value in prices) or volume is None or volume <= 0:
        raise ValueError(f"PEAD execution OHLCV is incomplete or invalid: {code}")
    opening, high, low, close = prices
    if not low <= min(opening, close) <= max(opening, close) <= high:
        raise ValueError(f"PEAD execution OHLC range is invalid: {code}")
