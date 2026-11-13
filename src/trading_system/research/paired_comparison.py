"""Same-session comparison of fixed vs daily names using the same received Rakuten book."""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

from trading_system.brokers.locking import single_writer
from trading_system.research import intraday_replay as replay
from trading_system.research.daytrade_universe import calendar_days, panel_location
from trading_system.research_panel import PROJECT
from trading_system.storage.store import atomic_write

FIRST = date(2026, 11, 16)


def coverage(db, day, signals, quotes):
    report = []

    def state(rows):
        if any(r["elapsed_minute"] and r["volume"] and r["o"] and r["c"] for r in rows):
            return "priced"
        if rows and all(r["elapsed_minute"] and r["volume"] == 0 and r["o"] is None for r in rows):
            return "zero"
        return "unknown"

    for session, decisions in zip(replay.SESSIONS, signals, strict=True):
        start, at, exit_at = [replay.stamp(day, t) for t in session]
        dates = [replay.fmt(start)] + [
            replay.fmt(at - timedelta(minutes=m)) for m in range(5, 0, -1)
        ]
        minute = {}
        for row in db.execute(
            """SELECT * FROM minute_bars WHERE source='rakuten' AND known_at<=?
            AND bar_at IN (?,?,?,?,?,?) ORDER BY known_at,source_id""",
            (replay.fmt(at), *dates),
        ):
            minute[row["symbol"], row["bar_at"]] = row
        observed = set()
        entry = set()
        exit_books = set()
        for q in db.execute(
            """SELECT DISTINCT symbol FROM quotes WHERE source='rakuten'
                      AND known_at>=? AND known_at<?""",
            (replay.fmt(start), replay.fmt(at)),
        ):
            observed.add(q[0])
        for q in quotes:
            if (
                q["read_started_at"] >= replay.fmt(at)
                and q["known_at"] <= replay.fmt(at + timedelta(minutes=5))
                and q["ask_at"] > replay.fmt(at)
                and q["ask_qty"] >= 100
            ):
                entry.add(q["symbol"])
            if (
                q["read_started_at"] >= replay.fmt(exit_at)
                and q["bid_at"] > replay.fmt(exit_at)
                and q["bid_qty"] >= 100
            ):
                exit_books.add(q["symbol"])
        for sig in decisions:
            c = sig["symbol"]
            first = state([minute[c, dates[0]]]) if (c, dates[0]) in minute else "unknown"
            tail = [minute[c, t] for t in dates[1:] if (c, t) in minute]
            recent = state(tail)
            if recent == "zero" and len(tail) < 5:
                recent = "unknown"
            if "zero" in (first, recent):
                status = "no_trade_minute"
            elif first == recent == "priced":
                status = "signal_observed"
            else:
                status = "missing_bars"
            report.append(
                dict(
                    symbol=c,
                    decision=session[1],
                    input_state=status,
                    quote_recorded=c in observed,
                    eligible_entry_book=c in entry,
                    eligible_exit_book=c in exit_books,
                )
            )
    return report


def compare(project, through, *, sessions=None):
    root = project / "data"
    folder = root / "research/paired-comparison"
    if sessions is None:
        cal, _ = calendar_days(root / "jquants", FIRST, datetime.now(UTC))
        # calendar_days loads a bounded window, including longer-running studies in chunks.
        cursor = FIRST
        while cursor + timedelta(days=20) < through:
            cursor += timedelta(days=20)
            more, _ = calendar_days(root / "jquants", cursor, datetime.now(UTC))
            cal.update(more)
        sessions = []
        cursor = FIRST
        while cursor <= through:
            if cal[str(cursor)] in ("1", "2"):
                sessions.append(str(cursor))
            cursor += timedelta(days=1)
    if not sessions:
        raise ValueError("no_comparison_sessions")
    coverage_log = {}
    gaps = []
    inputs = {}

    def stream(arm):
        for day in sessions:
            path = root / f"research/shared-market/{day}.sqlite"
            plan = root / f"rss-panel/daily/{day}/selection.json"
            records = {}
            if path.is_file() and plan.is_file():
                cfg = panel_location(root / "rss-panel", date.fromisoformat(day))
                raw = (cfg / "selection.json").read_bytes()
                roster = json.loads(raw)
                if "fixed_symbols" in roster:
                    info = path.stat()
                    identity = (info.st_size, info.st_mtime_ns, info.st_ino)
                    arms = {
                        "fixed": [s[:-2] for s in roster["fixed_symbols"]],
                        "daily": [r["code"][:4] for r in roster["selected"]],
                    }
                    with closing(
                        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                    ) as db:
                        db.row_factory = sqlite3.Row
                        db.execute("BEGIN")
                        books = replay.quotes(db, day)
                        for name, codes in arms.items():
                            if name == arm:
                                sig = [
                                    replay.read_signals(db, codes, day, s) for s in replay.SESSIONS
                                ]
                                payload = (day, sig, books)
                                records[arm] = coverage(db, day, sig, books)
                    records["roster_sha256"] = hashlib.sha256(raw).hexdigest()
                    with path.open("rb") as handle:
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    after = path.stat()
                    if (
                        identity != (after.st_size, after.st_mtime_ns, after.st_ino)
                        or inputs.setdefault(day, digest) != digest
                    ):
                        raise ValueError("comparison_inputs_changed")
            if not records:
                payload = (day, [[], []], [])
                if day not in gaps:
                    gaps.append(day)
            coverage_log.setdefault(day, {}).update(records)
            yield payload

    with single_writer(folder):
        result = {
            "through": str(through),
            "broker": "rakuten",
            "capital_per_arm": 1000000,
            "days": sessions,
            "missing_days": gaps,
            "arms": {},
            "coverage": coverage_log,
            "shared_database_sha256": inputs,
        }
        for arm in ("fixed", "daily"):
            stats, decisions, fills, trades, ledger = replay.replay(stream(arm))
            missing = sum(
                r["input_state"] == "missing_bars"
                for v in coverage_log.values()
                for r in v.get(arm, [])
            )
            absent_books = sum(
                not r["quote_recorded"] for v in coverage_log.values() for r in v.get(arm, [])
            )
            no_entry = stats["decisions"].get("no_qualifying_entry_quote", 0)
            result["arms"][arm] = dict(
                stats=stats,
                decisions=decisions,
                fills=fills,
                trades=trades,
                ledger=ledger,
                missing_decisions=missing,
                unobserved_books=absent_books,
                complete=not (
                    gaps or missing or absent_books or no_entry or stats["final_open_positions"]
                ),
            )
        valid = all(m["complete"] for m in result["arms"].values())
        result["state"] = "comparable" if valid else "partial"
        result["daily_minus_fixed_yen"] = (
            (
                result["arms"]["daily"]["stats"]["net_closed"]
                - result["arms"]["fixed"]["stats"]["net_closed"]
            )
            if valid
            else None
        )
        lines = [f"{through} までの楽天・同日比較（各組100万円・費用込み）"]
        for arm, label in (("fixed", "固定30"), ("daily", "日替わり30")):
            info = result["arms"][arm]
            stats = info["stats"]
            lines.append(
                f"{label}　観測分の決済損益 {stats['net_closed']:,.2f}円　"
                f"{stats['round_trips']}往復　{stats['wins']}勝 {stats['losses']}敗　"
                f"判断足不足 {info['missing_decisions']}件　"
                f"未決済 {stats['final_open_positions']}件"
            )
        if not gaps:
            diff = (
                result["arms"]["daily"]["stats"]["net_closed"]
                - result["arms"]["fixed"]["stats"]["net_closed"]
            )
            result["observed_realized_difference"] = diff
            lines.append(f"観測できた範囲の決済損益差（日替わり－固定） {diff:,.2f}円")
        lines.append(
            "対象期間の入力・決済を確認しました。"
            if valid
            else "欠測・気配不足・未決済を含むため、両組の優劣は判定保留です。"
        )
        dest = folder / str(through)
        atomic_write(
            dest / "result.json", json.dumps(result, ensure_ascii=False, default=str).encode()
        )
        atomic_write(dest / "比較結果.txt", ("\n".join(lines) + "\n").encode("utf-8-sig"))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--through", type=date.fromisoformat, required=True)
    args = p.parse_args()
    print(compare(PROJECT, args.through)["state"])
