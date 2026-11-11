"""Manual, read-only Rakuten minute-bar and bid/ask research recorder."""

import argparse
import gzip
import json
import math
import os
import re
import sqlite3
import subprocess
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_system.brokers.locking import single_writer
from trading_system.research_workbook import validate_workbook
from trading_system.rss_stop import can_start
from trading_system.storage.store import atomic_write, canonical

JST = ZoneInfo("Asia/Tokyo")
PROJECT = Path(__file__).resolve().parents[2]
BOOK_NAME = "研究用分足.xlsx"
SYMBOL = re.compile(r"[0-9][0-9A-Z]{3}\.T")


def panel_symbols(config):
    symbols = config["symbols"]
    if (
        config["version"] != 1
        or len(symbols) != 31
        or len(set(symbols)) != 31
        or any(not SYMBOL.fullmatch(s) for s in symbols)
        or "7203.T" not in symbols
    ):
        raise ValueError("invalid_panel_configuration")
    return symbols


def configuration(root):
    try:
        config = json.loads((root / "panel.json").read_bytes())
        symbols = panel_symbols(config)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("invalid_panel_configuration") from exc
    book = root / BOOK_NAME
    validate_workbook(book, symbols)
    return symbols, book


FAILURE_MESSAGES = {
    "invalid_panel_configuration": "31銘柄の設定ファイルを確認してください。",
    "research_workbook_unreadable": "研究用Excelを読み取れません。保存先を確認してください。",
    "invalid_research_workbook": "研究用Excelのファイル形式を確認してください。",
    "research_workbook_layout_changed": "研究用Excelの銘柄・シート・見出しが設定と一致しません。",
    "research_workbook_formulas_changed": "研究用Excelの取得用数式が設定と一致しません。",
    "research_storage_error": "研究データの保存先を確認してください。",
    "research_start_failed": "研究取得を開始・保存できません。Excelと保存先を確認してください。",
}


def failure_reason(exc):
    if isinstance(exc, ValueError) and str(exc) in FAILURE_MESSAGES:
        return str(exc)
    if isinstance(exc, sqlite3.Error):
        return "research_storage_error"
    return "research_start_failed"


def capture(book, symbols, rows):
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        p = subprocess.run(
            [
                str(shell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(PROJECT / "scripts/read_research_panel.ps1"),
                "-WorkbookPath",
                str(book),
                "-Symbols",
                ",".join(symbols),
                "-Rows",
                str(rows),
            ],
            capture_output=True,
            timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"state": "unavailable", "reason": "reader_timeout", "stage": "process"}
    except OSError:
        return {"state": "unavailable", "reason": "reader_launch_failed", "stage": "process"}
    if p.returncode:
        return {"state": "unavailable", "reason": "reader_exit_failed", "stage": "process"}
    try:
        data = json.loads(p.stdout.decode("utf-8-sig"))
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("state") in {"read", "unavailable"}:
        return data
    return {"state": "unavailable", "reason": "reader_invalid_output", "stage": "process"}


def excel_date(value):
    if isinstance(value, str):
        return date.fromisoformat(value.strip().replace("/", "-"))
    if type(value) in (int, float) and 36526 <= value <= 73000 and int(value) == value:
        return date(1899, 12, 30) + timedelta(days=int(value))
    raise ValueError("invalid_date")


def excel_clock(value):
    if isinstance(value, str) and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", value.strip()):
        return datetime.strptime(
            value.strip(), "%H:%M:%S" if value.count(":") == 2 else "%H:%M"
        ).time()
    if type(value) in (int, float) and 0 <= value < 1:
        return (datetime(2000, 1, 1) + timedelta(seconds=round(value * 86400))).time()
    raise ValueError("invalid_clock")


def number(v):
    if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
        raise ValueError("invalid_number")
    return v


def quote_state(q, end):
    try:
        day = excel_date(q[0])
        if day != end.astimezone(JST).date():
            return "other_quote_date"
        if not (0 < number(q[3]) <= number(q[4]) and number(q[5]) > 0 and number(q[6]) > 0):
            return "missing_or_crossed_book"
        for clock in q[7:9]:
            delay = (end - datetime.combine(day, excel_clock(clock), JST)).total_seconds()
            if not 0 <= delay <= 60:
                return "stale_or_future_book"
    except (ValueError, TypeError, OverflowError):
        return "invalid_book_fields"
    return "observed_book"


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS frames(
            id INTEGER PRIMARY KEY, started_at TEXT, known_at TEXT, state TEXT, raw_gzip BLOB);
        CREATE TABLE IF NOT EXISTS quotes(
            frame INTEGER, symbol TEXT, quality TEXT, fields_json TEXT, PRIMARY KEY(frame,symbol));
        CREATE TABLE IF NOT EXISTS market_indexes(
            frame INTEGER, code TEXT, fields_json TEXT, quality TEXT, PRIMARY KEY(frame,code));
        CREATE TABLE IF NOT EXISTS bars(
            frame INTEGER, symbol TEXT, bar_time TEXT, elapsed_minute INTEGER,
            o REAL, h REAL, l REAL, c REAL, volume REAL, PRIMARY KEY(symbol,bar_time,frame));
    """)


def save(db, symbols, data, start, end):
    items = data.get("items", [])
    if data.get("state") == "read" and (
        not isinstance(items, list)
        or any(not isinstance(x, dict) for x in items)
        or [x.get("symbol") for x in items] != symbols
        or any(
            not isinstance(x.get("quote"), list)
            or len(x["quote"]) != 10
            or not isinstance(x.get("bars"), list)
            or len(x["bars"]) > 420
            for x in items
        )
    ):
        state = "panel_mismatch"
    else:
        state = data.get("state", "unavailable")
    if end < start:
        state = "clock_regression"
    counts = {
        "books": 0,
        "bar_versions": 0,
        "valid_bars_seen": 0,
        "invalid_bars": 0,
        "empty_price_minutes": 0,
        "chart_symbols": 0,
        "current_day_chart_symbols": 0,
        "latest_chart_at": None,
    }
    local_day = end.astimezone(JST).date().isoformat()
    with db:
        frame = db.execute(
            "INSERT INTO frames(started_at,known_at,state,raw_gzip) VALUES(?,?,?,?)",
            (
                start.isoformat(),
                end.isoformat(),
                state,
                gzip.compress(canonical(data).encode(), mtime=0),
            ),
        ).lastrowid
        if state == "read":
            for index in data.get("indexes", []):
                fields = index["fields"]
                if index["code"] not in ("N225", "TOPX") or len(fields) != 3:
                    continue
                try:
                    day = excel_date(fields[0])
                    ts = datetime.combine(day, excel_clock(fields[1]), JST)
                    valid = (
                        day == end.astimezone(JST).date() and ts <= end and number(fields[2]) > 0
                    )
                except (ValueError, TypeError, OverflowError):
                    valid = False
                db.execute(
                    "INSERT INTO market_indexes VALUES(?,?,?,?)",
                    (frame, index["code"], canonical(fields), "observed" if valid else "attention"),
                )
        for x in items if state == "read" else []:
            q = x["quote"]
            symbol = x["symbol"]
            quality = quote_state(q, end)
            db.execute("INSERT INTO quotes VALUES(?,?,?,?)", (frame, symbol, quality, canonical(q)))
            counts["books"] += int(quality == "observed_book")
            parsed = {}
            conflicts = set()
            for row in x["bars"]:
                if isinstance(row, list) and all(v in (None, "", "--------") for v in row):
                    continue
                try:
                    if not isinstance(row, list) or len(row) != 7:
                        raise ValueError("width")
                    at = datetime.combine(excel_date(row[0]), excel_clock(row[1]), JST)
                    if at.second or at > end:
                        raise ValueError("invalid_bar")
                    volume = number(row[6])
                    if volume == 0 and all(v in ("", None) for v in row[2:6]):
                        opening = high = low = close = None
                    else:
                        opening, high, low, close = map(number, row[2:6])
                        if not 0 < low <= min(opening, close) <= max(opening, close) <= high:
                            raise ValueError("invalid_bar")
                    key = at.isoformat()
                    payload = (
                        int(at + timedelta(minutes=1) <= end),
                        opening,
                        high,
                        low,
                        close,
                        volume,
                    )
                    if key in parsed and parsed[key] != payload:
                        conflicts.add(key)
                    parsed[key] = payload
                except (ValueError, TypeError, OverflowError):
                    counts["invalid_bars"] += 1
            counts["invalid_bars"] += len(conflicts)
            counts["chart_symbols"] += int(len(parsed) > len(conflicts))
            latest = max((k for k in parsed if k not in conflicts), default=None)
            if latest is not None:
                counts["current_day_chart_symbols"] += int(latest[:10] == local_day)
                if counts["latest_chart_at"] is None or latest > counts["latest_chart_at"]:
                    counts["latest_chart_at"] = latest
            for key, values in parsed.items():
                if key in conflicts:
                    continue
                counts["empty_price_minutes" if values[1] is None else "valid_bars_seen"] += 1
                old = db.execute(
                    "SELECT elapsed_minute,o,h,l,c,volume FROM bars "
                    "WHERE symbol=? AND bar_time=? ORDER BY frame DESC LIMIT 1",
                    (symbol, key),
                ).fetchone()
                if old != values:
                    db.execute(
                        "INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)", (frame, symbol, key, *values)
                    )
                    counts["bar_versions"] += 1
    return {"state": state, "reason": data.get("reason"), **counts}


def run(
    root,
    requested,
    resume,
    *,
    one_pass=False,
    clock=lambda: datetime.now(UTC),
    sleep=time.sleep,
    opener=None,
    reader=capture,
):
    start_day = datetime.fromtimestamp(requested / 1e9, JST)
    cutoff = start_day.replace(hour=15, minute=59, second=0, microsecond=0)
    deadline = cutoff + timedelta(minutes=1)

    def stopped():
        if (root / "STOP").exists():
            return "manual_stop"
        if not one_pass and clock() >= deadline:
            return "daytime_1600"
        return None

    def status(**data):
        atomic_write(
            root / "status.json",
            canonical(dict(updated_at=clock().isoformat(), **data)).encode(),
            sharing_retries=50,
        )

    with single_writer(root):
        local_now = clock().astimezone(JST)
        if local_now.date() != start_day.date() or (
            not one_pass and (local_now.hour < 6 or clock() >= cutoff)
        ):
            status(state="not_started", reason="outside_start_hours")
            return
        if not can_start(root, resume):
            status(state="not_started", reason="stop_requested")
            return
        try:
            symbols, book = configuration(root)
            if stopped() or (not one_pass and clock() >= cutoff):
                status(state="not_started", reason="stop_or_cutoff")
                return
            status(state="starting", frames=0)
            (opener or os.startfile)(str(book))
            db = sqlite3.connect(root / "research.sqlite")
            try:
                initialize(db)
                count = 0
                next_full = 0
                fast_retry_used = False
                result = {}
                while not (reason := stopped()):
                    begin = clock()
                    rows = 420 if one_pass or count >= next_full else 12
                    data = {**reader(book, symbols, rows), "requested_rows": rows}
                    result = save(db, symbols, data, begin, clock())
                    success = result["state"] == "read"
                    if rows == 420:
                        # Keep one short poll between failed full reads so live quotes can progress.
                        next_full = count + (15 if success else 2)
                    count += 1
                    status(**result, frames=count)
                    if one_pass and (result["chart_symbols"] == len(symbols) or count >= 4):
                        reason = (
                            "one_pass_complete"
                            if result["chart_symbols"] == len(symbols)
                            else "incomplete_panel"
                        )
                        break
                    if success:
                        fast_retry_used = False
                    wait_seconds = 15
                    if (
                        not fast_retry_used
                        and result["reason"]
                        in {
                            "busy",
                            "excel_call_rejected",
                            "excel_retry_later",
                            "excel_unavailable",
                            "excel_property_unavailable",
                            "excel_range_shape",
                            "chart_range_changed",
                        }
                        and result["state"] == "unavailable"
                    ):
                        wait_seconds = 3
                        fast_retry_used = True
                    for _ in range(2 * wait_seconds):
                        if stopped():
                            break
                        sleep(0.5)
                status(
                    **{k: v for k, v in result.items() if k not in {"state", "reason"}},
                    state="needs_review" if reason == "incomplete_panel" else "stopped",
                    reason=reason,
                    last_read_reason=result.get("reason"),
                    frames=count,
                )
            finally:
                db.close()
        except Exception as exc:
            status(state="failed", reason=failure_reason(exc))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requested", type=int, required=True)
    parser.add_argument("--resume", default="none")
    parser.add_argument("--snapshot-now", action="store_true")
    args = parser.parse_args()
    run(PROJECT / "data/rss-panel", args.requested, args.resume, one_pass=args.snapshot_now)
    from trading_system.research.shared_market import after_stop

    after_stop(PROJECT, datetime.fromtimestamp(args.requested / 1e9, JST).date(), "rakuten")


if __name__ == "__main__":
    main()
