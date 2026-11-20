"""Common, local research tables for Rakuten RSS and kabuStation. No broker connections."""

import argparse
import gzip
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from trading_system.brokers.locking import single_writer
from trading_system.research_panel import (
    JST,
    PROJECT,
    excel_clock,
    excel_date,
    initialize,
    number,
    save,
)
from trading_system.storage.store import atomic_write, canonical


def utc(value):
    if not value:
        return None
    t = datetime.fromisoformat(value)
    if t.utcoffset() is None:
        return None
    return t.astimezone(UTC).isoformat(timespec="microseconds")


def rss_time(day, clock):
    try:
        return (
            datetime.combine(excel_date(day), excel_clock(clock), JST)
            .astimezone(UTC)
            .isoformat(timespec="microseconds")
        )
    except (ValueError, TypeError, OverflowError):
        return None


def numeric(value, *, zero=True):
    try:
        n = number(value)
        return n if n or zero else None
    except (ValueError, OverflowError):
        return None


def insert_quote(
    db,
    src,
    seq,
    code,
    start,
    end,
    lasttime,
    last,
    volume,
    bid,
    ask,
    bq,
    aq,
    bt,
    at,
    quality,
    value=None,
    vwap=None,
):
    db.execute(
        "INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            src,
            seq,
            code,
            code + "0",
            utc(start),
            utc(end),
            lasttime,
            numeric(last, zero=False),
            numeric(volume),
            numeric(bid, zero=False),
            numeric(ask, zero=False),
            numeric(bq),
            numeric(aq),
            bt,
            at,
            quality,
            "TSE",
            numeric(value),
            numeric(vwap, zero=False),
        ),
    )


def import_rakuten(src, dest, day):
    # Replay original frames using the recorder's validation, including explicit zero-volume
    # minutes in older frames. Preserve the original receive time, source frame and revisions.
    with closing(sqlite3.connect(":memory:")) as buf:
        initialize(buf)
        for fid, start, end, state, blob in src.execute(
            "SELECT id,started_at,known_at,state,raw_gzip FROM frames "
            "WHERE utc_time(known_at)>=? AND utc_time(known_at)<? ORDER BY id",
            day,
        ):
            payload = json.loads(gzip.decompress(blob))
            dest.execute(
                "INSERT INTO capture_status VALUES(?,?,?,?,?)",
                ("rakuten", fid, utc(end), state, payload.get("reason")),
            )
            if state != "read":
                continue
            symbols = [i["symbol"] for i in payload["items"]]
            if not symbols or len(set(symbols)) != len(symbols):
                raise ValueError("invalid_source_symbols")
            save(buf, symbols, payload, datetime.fromisoformat(start), datetime.fromisoformat(end))
            frame = buf.execute("SELECT max(id) FROM frames").fetchone()[0]
            for sym, qstate, txt in buf.execute(
                "SELECT symbol,quality,fields_json FROM quotes WHERE frame=?", (frame,)
            ):
                q = json.loads(txt)
                code = sym.removesuffix(".T")
                bt = rss_time(q[0], q[7])
                at = rss_time(q[0], q[8])
                insert_quote(
                    dest,
                    "rakuten",
                    fid,
                    code,
                    start,
                    end,
                    rss_time(q[0], q[2]),
                    q[1],
                    q[9],
                    q[3],
                    q[4],
                    q[5],
                    q[6],
                    bt,
                    at,
                    qstate,
                )
                for side, px, qty, ts in (("buy", q[3], q[5], bt), ("sell", q[4], q[6], at)):
                    if numeric(px, zero=False) is not None and numeric(qty) is not None:
                        dest.execute(
                            "INSERT INTO book_levels VALUES(?,?,?,?,?,?,?,?,?)",
                            ("rakuten", fid, code, utc(end), side, 1, px, qty, ts),
                        )
            for sym, bar_time, elapsed, opening, high, low, close, volume in buf.execute(
                "SELECT symbol,bar_time,elapsed_minute,o,h,l,c,volume FROM bars WHERE frame=?",
                (frame,),
            ):
                dest.execute(
                    "INSERT INTO minute_bars VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "rakuten",
                        fid,
                        sym.removesuffix(".T"),
                        utc(end),
                        utc(bar_time),
                        elapsed,
                        opening,
                        high,
                        low,
                        close,
                        volume,
                    ),
                )
            for code, fields, quality in buf.execute(
                "SELECT code,fields_json,quality FROM market_indexes WHERE frame=?", (frame,)
            ):
                dt, clock, price = json.loads(fields)
                dest.execute(
                    "INSERT INTO market_indexes VALUES(?,?,?,?,?,?,?)",
                    (
                        "rakuten",
                        fid,
                        code,
                        utc(end),
                        rss_time(dt, clock),
                        numeric(price, zero=False),
                        quality,
                    ),
                )
            buf.execute("UPDATE frames SET raw_gzip=x'' WHERE id=?", (frame,))
            buf.commit()


def import_kabu(src, dest, day):
    # One row per actual request: a failed request never silently disappears in a join.
    for row in src.execute(
        "SELECT id,symbol,operation,started_at,known_at,quality,trade_time,price,"
        "cumulative_volume,trading_value,vwap FROM samples "
        "WHERE utc_time(known_at)>=? AND utc_time(known_at)<? ORDER BY id",
        day,
    ):
        seq, code, op, start, end, quality, trade_time, last, volume, value, vwap = row
        dest.execute(
            "INSERT INTO capture_status VALUES(?,?,?,?,?)",
            ("mufg_esmart", seq, utc(end), op, quality),
        )
        if op != "board":
            continue
        best = {}
        for side, depth, px, qty, ts in src.execute(
            "SELECT side,depth,price,quantity,reported_time FROM levels WHERE sample=?", (seq,)
        ):
            name = {"Buy": "buy", "Sell": "sell"}[side]
            try:
                t = utc(ts)
            except (TypeError, ValueError):
                t = None
            dest.execute(
                "INSERT INTO book_levels VALUES(?,?,?,?,?,?,?,?,?)",
                ("mufg_esmart", seq, code, utc(end), name, depth, px, qty, t),
            )
            if depth == 1:
                best[name] = (px, qty, t)
        bid, bq, bt = best.get("buy", (None, None, None))
        ask, aq, at = best.get("sell", (None, None, None))
        try:
            lasttime = utc(trade_time)
        except (TypeError, ValueError):
            lasttime = None
        insert_quote(
            dest,
            "mufg_esmart",
            seq,
            code,
            start,
            end,
            lasttime,
            last,
            volume,
            bid,
            ask,
            bq,
            aq,
            bt,
            at,
            quality,
            value,
            vwap,
        )


def latest_asof(db, when):
    if when.utcoffset() is None:
        raise ValueError("aware_decision_time_required")
    cutoff = when.astimezone(UTC).isoformat(timespec="microseconds")
    # Latest observation per provider, even if rejected. Never borrow a later quote.
    return db.execute(
        "SELECT * FROM (SELECT q.*, row_number() OVER (PARTITION BY source,symbol "
        "ORDER BY known_at DESC,source_id DESC) AS seq_rank FROM quotes q WHERE known_at<=?) "
        "WHERE seq_rank=1 ORDER BY symbol,source",
        (cutoff,),
    ).fetchall()


def publish(project, day):
    root = project / "data"
    target = root / "research/shared-market"
    target.mkdir(parents=True, exist_ok=True)
    begin = datetime.combine(day, datetime.min.time(), JST).astimezone(UTC)
    bounds = (utc(begin.isoformat()), utc((begin + timedelta(days=1)).isoformat()))
    paths = [
        ("rakuten", root / "rss-panel/research.sqlite", import_rakuten),
        ("mufg_esmart", root / "mufg-panel/research.sqlite", import_kabu),
    ]
    with single_writer(target), TemporaryDirectory(dir=target) as tmp:
        file = Path(tmp) / "study.sqlite"
        with closing(sqlite3.connect(file)) as db:
            db.executescript("""
                CREATE TABLE quotes(source TEXT,source_id INTEGER,symbol TEXT,jquants_code TEXT,
                  read_started_at TEXT,known_at TEXT,trade_at TEXT,last REAL,day_volume REAL,
                  bid REAL,ask REAL,bid_qty REAL,ask_qty REAL,bid_at TEXT,ask_at TEXT,
                  source_quality TEXT,exchange TEXT,trading_value REAL,vwap REAL,
                  PRIMARY KEY(source,source_id,symbol));
                CREATE INDEX quotes_asof ON quotes(symbol,source,known_at,source_id);
                CREATE TABLE book_levels(source TEXT,source_id INTEGER,symbol TEXT,known_at TEXT,
                  side TEXT,depth INTEGER,price REAL,qty REAL,source_at TEXT,
                  PRIMARY KEY(source,source_id,symbol,side,depth));
                CREATE TABLE minute_bars(source TEXT,source_id INTEGER,symbol TEXT,known_at TEXT,
                  bar_at TEXT,elapsed_minute INTEGER,o REAL,h REAL,l REAL,c REAL,volume REAL,
                  PRIMARY KEY(source,source_id,symbol,bar_at));
                CREATE TABLE market_indexes(source TEXT,source_id INTEGER,code TEXT,known_at TEXT,
                  source_at TEXT,value REAL,source_quality TEXT,PRIMARY KEY(source,source_id,code));
                CREATE TABLE capture_status(source TEXT,source_id INTEGER,known_at TEXT,state TEXT,
                  reason TEXT,PRIMARY KEY(source,source_id));
                CREATE TABLE sources(source TEXT PRIMARY KEY,available INTEGER,
                  quote_observations INTEGER);
            """)
            with db:
                for label, path, loader in paths:
                    exists = path.is_file()
                    if exists:
                        with closing(sqlite3.connect(Path(tmp) / (label + ".sqlite"))) as snap:
                            with closing(
                                sqlite3.connect(
                                    path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
                                )
                            ) as src:
                                src.backup(snap, pages=256, sleep=0.05)
                            snap.create_function("utc_time", 1, utc, deterministic=True)
                            loader(snap, db, bounds)
                    cnt = db.execute(
                        "SELECT count(*) FROM quotes WHERE source=?", (label,)
                    ).fetchone()[0]
                    db.execute("INSERT INTO sources VALUES(?,?,?)", (label, int(exists), cnt))
            if not db.execute("SELECT count(*) FROM capture_status").fetchone()[0]:
                raise ValueError("no_records_for_day")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("shared_db_failed_integrity_check")
        dest = target / (day.isoformat() + ".sqlite")
        os.replace(file, dest)
    return dest


def after_stop(project, day, broker):
    """A concurrent second finisher publishes again, including both final commits."""
    target = project / "data/research/shared-market"
    for attempt in range(61):
        try:
            path = publish(project, day)
            result = {"state": "published", "file": path.name}
            break
        except ValueError as exc:
            if str(exc) == "broker_state_already_open" and attempt < 60:
                time.sleep(1)
                continue
            result = {
                "state": "not_published",
                "reason": str(exc)
                if str(exc) in ("no_records_for_day", "broker_state_already_open")
                else "invalid_input",
            }
            break
        except (OSError, sqlite3.Error):
            result = {"state": "not_published", "reason": "check_storage_or_close_analysis_reader"}
            break
    atomic_write(
        target / (broker + "-publish.json"),
        canonical({**result, "date": str(day), "at": datetime.now(UTC).isoformat()}).encode(),
        sharing_retries=50,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(JST).date())
    args = parser.parse_args()
    print(publish(PROJECT, args.date))


if __name__ == "__main__":
    main()
