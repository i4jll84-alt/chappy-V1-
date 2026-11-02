"""Operator-started kabuStation panel. One local token, bounded round-robin board reads only."""

import argparse
import gzip
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from trading_system.brokers.locking import single_writer
from trading_system.brokers.mufg_esmart.capture import password_prompt
from trading_system.brokers.mufg_esmart.market_data import Client
from trading_system.research_panel import JST, PROJECT, number, panel_symbols
from trading_system.rss_stop import can_start
from trading_system.storage.store import atomic_write, canonical


def tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS samples(
          id INTEGER PRIMARY KEY, symbol TEXT, operation TEXT, started_at TEXT, known_at TEXT,
          http_status INTEGER, quality TEXT, trade_time TEXT, price REAL, cumulative_volume REAL,
          trading_value REAL, vwap REAL, raw_board_gzip BLOB);
        CREATE INDEX IF NOT EXISTS samples_by_code ON samples(symbol,known_at);
        CREATE TABLE IF NOT EXISTS levels(
          sample INTEGER, side TEXT, depth INTEGER, price REAL, quantity REAL, reported_time TEXT,
          PRIMARY KEY(sample,side,depth));
    """)


def numeric(v):
    try:
        return number(v)
    except (ValueError, OverflowError):
        return None


def store_sample(db, record, raw):
    status = record["http_status"]
    symbol = record["code"]
    op = record["operation"]
    known = datetime.fromisoformat(record["read_completed_at"])
    board = {}
    quality = record["reason"] or record["outcome"]
    levels = []
    safe_raw = raw if op == "board" and status == 200 else None
    if raw:
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            value = None
        if not isinstance(value, dict):
            quality = "invalid_board"
        elif status != 200 and value.get("Code") == 4001018:
            quality = "registration_limit"
        elif status == 200 and record["outcome"] == "ok":
            if (
                value.get("Symbol") != symbol
                or type(value.get("Exchange")) is not int
                or value["Exchange"] != 1
            ):
                quality = "unexpected_symbol_or_exchange"
            else:
                board = value
                for side in ("Buy", "Sell"):
                    for depth in range(1, 11):
                        lvl = board.get(side + str(depth))
                        if not isinstance(lvl, dict):
                            continue
                        px = numeric(lvl.get("Price"))
                        qty = numeric(lvl.get("Qty"))
                        if px and qty is not None:
                            levels.append((side, depth, px, qty, lvl.get("Time")))
                best = {side: px for side, depth, px, qty, _ in levels if depth == 1 and qty > 0}
                quality = (
                    "observed_book"
                    if len(best) == 2 and best["Buy"] <= best["Sell"]
                    else "incomplete_or_crossed_book"
                )
    if known < datetime.fromisoformat(record["read_started_at"]):
        quality = "clock_regression"
        board = {}
        levels = []
    with db:
        frame = db.execute(
            "INSERT INTO samples(symbol,operation,started_at,known_at,http_status,quality,"
            "trade_time,price,cumulative_volume,trading_value,vwap,raw_board_gzip) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                symbol,
                op,
                record["read_started_at"],
                known.isoformat(),
                status,
                quality,
                board.get("CurrentPriceTime"),
                numeric(board.get("CurrentPrice")),
                numeric(board.get("TradingVolume")),
                numeric(board.get("TradingValue")),
                numeric(board.get("VWAP")),
                gzip.compress(safe_raw, mtime=0) if safe_raw else None,
            ),
        ).lastrowid
        db.executemany(
            "INSERT INTO levels VALUES(?,?,?,?,?,?)", [(frame, *level) for level in levels]
        )
    return quality


def run(
    project,
    requested,
    resume,
    *,
    one_pass=False,
    clock=lambda: datetime.now(UTC),
    sleep=time.sleep,
    prompt=password_prompt,
    client_factory=Client,
):
    root = project / "data/mufg-panel"
    shared = project / "data/mufg-continuous"
    requested_day = datetime.fromtimestamp(requested / 1e9, JST)
    deadline = requested_day.replace(hour=16, minute=0, second=0, microsecond=0)
    prompt_timeout = time.monotonic() + 300

    def stopped():
        if (root / "STOP").exists():
            return "manual_stop"
        if not one_pass and clock() >= deadline:
            return "daytime_1600"
        return None

    def status(state, reason=None, **counts):
        atomic_write(
            root / "status.json",
            canonical(
                dict(state=state, reason=reason, updated_at=clock().isoformat(), **counts)
            ).encode(),
            sharing_retries=50,
        )

    with single_writer(root):
        now = clock().astimezone(JST)
        if now.date() != requested_day.date() or (
            not one_pass and (now.hour < 6 or now >= deadline - timedelta(minutes=1))
        ):
            status("not_started", "outside_start_hours")
            return
        if not can_start(root, resume):
            status("not_started", "stop_requested")
            return
        try:
            with single_writer(shared):
                symbols = [
                    s[:-2]
                    for s in panel_symbols(
                        json.loads((project / "data/rss-panel/panel.json").read_bytes())
                    )
                ]
                status("prompt")
                if stopped():
                    status("stopped", stopped())
                    return

                def prompt_stop():
                    return stopped() or (one_pass and time.monotonic() >= prompt_timeout)

                password = prompt(prompt_stop, "研究用31銘柄の株価・板", one_pass=one_pass)
                if stopped() or not password:
                    status("stopped", stopped() or "prompt_cancelled")
                    return
                client = client_factory(password, board_symbols=symbols, clock=clock)
                password = None
                try:
                    db = sqlite3.connect(root / "research.sqlite")
                    try:
                        tables(db)
                        index = 0
                        reads = 0
                        failures = 0
                        last_quality = None
                        retry = []
                        observed = set()
                        while not (reason := stopped()):
                            code = (
                                retry[0] if one_pass and reads >= len(symbols) else symbols[index]
                            )
                            record, raw = client.read(code)
                            quality = store_sample(db, record, raw)
                            if record["operation"] == "board":
                                if quality == "observed_book":
                                    observed.add(code)
                                if one_pass:
                                    if reads >= len(symbols):
                                        retry.pop(0)
                                    elif record["retryable"]:
                                        retry.append(code)
                                index = (index + 1) % len(symbols)
                                reads += 1
                            failures = 0 if record["outcome"] == "ok" else failures + 1
                            if (
                                quality == "registration_limit"
                                or (
                                    record["operation"] == "token"
                                    and not record["retryable"]
                                    and record["outcome"] != "ok"
                                )
                                or failures >= len(symbols)
                            ):
                                reason = quality
                                break
                            if one_pass and reads >= len(symbols) and not retry:
                                reason = "one_pass_complete"
                                break
                            if reads % len(symbols) == 0 or last_quality != quality:
                                status(
                                    "recording",
                                    quality,
                                    board_reads=reads,
                                    last_symbol=record["code"],
                                )
                                last_quality = quality
                            wait = (
                                30
                                if record["http_status"] == 429
                                else min(8, 2 ** min(failures, 3))
                            )
                            for _ in range(wait * 2):
                                if stopped():
                                    break
                                sleep(0.5)
                        status(
                            "stopped"
                            if reason in ("manual_stop", "daytime_1600", "one_pass_complete")
                            else "needs_review",
                            reason,
                            board_reads=reads,
                            symbols_with_book=len(observed),
                        )
                    finally:
                        db.close()
                finally:
                    client.close()
        except Exception as exc:
            status(
                "failed",
                "collector_already_active"
                if str(exc) == "broker_state_already_open"
                else "check_local_capture",
            )
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--requested", type=int, required=True)
    p.add_argument("--resume", default="none")
    p.add_argument(
        "--snapshot-now", action="store_true", help="Manual one-pass read, including after close"
    )
    a = p.parse_args()
    run(PROJECT, a.requested, a.resume, one_pass=a.snapshot_now)
    from trading_system.research.shared_market import after_stop

    after_stop(PROJECT, datetime.fromtimestamp(a.requested / 1e9, JST).date(), "mufg_esmart")


if __name__ == "__main__":
    main()
