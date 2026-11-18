"""Prospective 100-share JSF swing account, using the verified uncapped morning replay rules."""

import importlib.util
import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from time import monotonic

import duckdb

from trading_system.research import swing_forward as swing
from trading_system.research.daytrade_universe import JST, calendar_days, dated_inputs

OPEN = time(9)
CLOSE = time(15, 30)
INPUT_ERRORS = (FileNotFoundError, ValueError, sqlite3.Error, duckdb.Error)
STRATEGY = "swing_s3_jsf_uncapped_morning_v1"


@lru_cache(maxsize=2)
def research_module(name):
    # The source checkout also holds the fixed-history replay scripts used by this plan.
    path = Path(swing.__file__).resolve().parents[3] / "scripts/swing_gap_atr" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serial(value):
    """Keep all monetary fields at the account engine's Decimal precision on disk."""
    return json.loads(json.dumps(value, default=str, allow_nan=False))


def initial(start, capital):
    if type(capital) is not int or capital <= 0:
        raise ValueError("initial_capital_must_be_a_positive_integer")
    return {
        "strategy": STRATEGY,
        "start": start,
        "initial_capital": str(capital),
        "cash": str(capital),
        "nav": str(capital),
        "positions": {},
        "realized": "0",
        "peak_nav": str(capital),
        "max_drawdown_pct": "0",
        "last_mark": None,
        "review_count": 0,
        "last_review": None,
    }


def schedule(project, signal, now):
    first = date.fromisoformat(signal)
    days, refs = calendar_days(project / "data/jquants", first, now)
    trading = sorted(d for d, flag in days.items() if d >= signal and flag in ("1", "2"))
    if len(trading) < 8 or trading[0] != signal:
        raise ValueError("signal_or_future_calendar_unavailable")
    span = (date.fromisoformat(trading[7]) - first).days
    if any((first + timedelta(days=i)).isoformat() not in days for i in range(span + 1)):
        raise ValueError("incomplete_future_calendar")
    return trading[1], trading[7], refs


def prepare(project, signal_date, target, due, cutoff):
    signal = swing.screen(project, date.fromisoformat(signal_date), cutoff)
    if not signal["credit_complete"]:
        reasons = [f"{r['date']}: {r['issue']}" for r in signal["credit_refs"] if "issue" in r]
        raise ValueError("jsf_incomplete: " + ", ".join(reasons))
    eligible = [r for r in signal["candidates"] if not r["plus3_exclusions"]][:5]
    replay = research_module("replay_recent")
    picks = []
    if eligible:
        history, _ = replay.historical_window(
            project / "data/jquants", signal, {r["code"] for r in eligible}
        )
        for rank, row in enumerate(eligible, 1):
            atr = replay.signal_atr(history, row["code"])
            picks.append(
                dict(
                    code=row["code"],
                    rank=rank,
                    prev_close=row["close"],
                    atr=str(atr) if atr and atr > 0 else None,
                    name=row["name"],
                    return20=row["return20"],
                )
            )
    return dict(
        strategy=STRATEGY,
        status="ready",
        signal_date=signal_date,
        target=target,
        due=due,
        cutoff=cutoff.isoformat(),
        picks=picks,
        price_and_master_sources=signal["input_refs"],
        jsf_sources=signal["credit_refs"],
    )


def preopen_stop(project, book, folder, decision, deadline, stamp):
    path = folder / "preopen_stop.json"
    if path.exists():
        notice = swing.read(path)
        observed = datetime.fromisoformat(notice["observed_at"])
        if (
            notice["target"] != decision["target"]
            or notice["decision_sha256"] != swing.signature(decision)
            or not datetime.fromisoformat(decision["recorded_at"]) <= observed < deadline
        ):
            raise ValueError("invalid_preopen_stop")
        return notice
    if decision["status"] != "ready" or not decision["picks"] or stamp() >= deadline:
        return None
    switches = {"book": book / "STOP", "swing": project / "data/swing-forward/STOP"}
    active = [name for name, switch in switches.items() if switch.exists()]
    observed = stamp()
    if not active or observed >= deadline:
        return None
    notice = dict(
        target=decision["target"],
        observed_at=observed.isoformat(),
        switches=active,
        decision_sha256=swing.signature(decision),
        canceled_buy_candidates=[r["code"] for r in decision["picks"]],
    )
    swing.write(path, notice)
    return notice


def settle(project, previous, decision, asof, stop_notice=None):
    engine = research_module("cap_backtest")
    day = decision["target"]
    if previous["last_mark"] not in (None, decision["signal_date"]):
        raise ValueError("nonconsecutive_account")
    acct = engine.Account("morning", None, "plan_a", cash=Decimal(previous["initial_capital"]))
    acct.cash = Decimal(previous["cash"])
    acct.realized = Decimal(previous["realized"])
    for code, raw in sorted(
        previous["positions"].items(), key=lambda pair: (pair[1]["entered"], pair[1]["rank"])
    ):
        values = {
            k: Decimal(v) if k in ("entry", "atr", "stop", "shares", "debit", "quote") else v
            for k, v in raw.items()
        }
        acct.positions[code] = engine.Lot(**values)
    picks = [
        dict(
            code=r["code"],
            rank=r["rank"],
            prev_close=Decimal(r["prev_close"]),
            atr=Decimal(r["atr"]) if r["atr"] is not None else None,
        )
        for r in ([] if stop_notice else decision["picks"])
    ]
    quotes, sources = {}, []
    if picks or acct.positions:
        columns, _, sources = dated_inputs(project / "data/jquants", [day], asof)
        if not columns or any(
            len(bars) != 1 or bars[0]["Date"] != day for bars in columns.values()
        ):
            raise ValueError("missing_or_invalid_settlement_cross_section")
        quotes = {code: rows[0] for code, rows in columns.items()}
    engine.run_day(acct, day, decision["due"], picks, quotes, None)
    mark = acct.nav[-1]
    high = max(Decimal(previous["peak_nav"]), mark["nav"])
    dd = min(Decimal(previous["max_drawdown_pct"]), 100 * (mark["nav"] / high - 1))
    state = dict(
        previous,
        cash=acct.cash,
        positions={c: asdict(p) for c, p in acct.positions.items()},
        nav=mark["nav"],
        peak_nav=high,
        max_drawdown_pct=dd,
        realized=acct.realized,
        last_mark=day,
    )
    flagged = [r for r in acct.exceptions if r["strict_break"]]
    if flagged:
        state["review_count"] += len(flagged)
        state["last_review"] = dict(date=day, issues=[(r["code"], r["kind"]) for r in flagged])
    return serial(
        dict(
            target=day,
            recorded_at=asof.isoformat(),
            previous_sha256=swing.signature(previous),
            decision_sha256=swing.signature(decision),
            preopen_stop_sha256=swing.signature(stop_notice) if stop_notice else None,
            account=state,
            mark=mark,
            events=acct.events,
            trades=acct.trades,
            exceptions=acct.exceptions,
            sources=sources,
        )
    )


def advance(project: Path, book: Path, *, start: str, capital: int, now: datetime) -> dict:
    """Advance one exclusively locked book. A session's frozen signal and close are write-once."""
    if now.tzinfo is None:
        raise ValueError("timezone_required")
    now = now.astimezone(JST)
    before = monotonic()

    def stamp():
        return now + timedelta(seconds=monotonic() - before)

    seed = initial(start, capital)
    path = book / "initial.json"
    if path.exists():
        if swing.read(path) != seed:
            raise ValueError("forward_account_configuration_changed")
    else:
        swing.write(path, seed)
    marks = sorted((book / "sessions").glob("????-??-??/close.json"))
    state = seed
    last_decision = None
    if marks:
        close = swing.read(marks[-1])
        state = close["account"]
        if (
            state["last_mark"] != marks[-1].parent.name
            or state["initial_capital"] != str(capital)
            or state["start"] != start
            or state["strategy"] != STRATEGY
        ):
            raise ValueError("invalid_last_close")
        prior = swing.read(marks[-1].with_name("decision.json"))
        if close["decision_sha256"] != swing.signature(prior):
            raise ValueError("frozen_decision_changed")
        if close.get("preopen_stop_sha256") and close["preopen_stop_sha256"] != swing.signature(
            swing.read(marks[-1].with_name("preopen_stop.json"))
        ):
            raise ValueError("preopen_stop_changed")
        last_decision = prior["signal_date"]
    signal = state["last_mark"] or start

    def status(name, target=None, decision=None, issue=None):
        return dict(
            **state,
            status=name,
            last_decision=last_decision,
            next_signal=signal,
            next_entry=target,
            picks=(decision or {}).get("picks", []),
            decision_status=(decision or {}).get("status"),
            decision_origin=(decision or {}).get("decision_origin"),
            decision_cutoff=(decision or {}).get("cutoff"),
            decision_recorded_at=(decision or {}).get("recorded_at"),
            issue=issue,
        )

    while True:
        try:
            target, due, calendar_refs = schedule(project, signal, stamp())
        except INPUT_ERRORS as exc:
            return status("waiting_calendar", issue=str(exc))
        folder = book / "sessions" / target
        path = folder / "decision.json"
        decide_from = datetime.combine(date.fromisoformat(signal), CLOSE, JST)
        deadline = datetime.combine(date.fromisoformat(target), OPEN, JST)
        mark_from = datetime.combine(date.fromisoformat(target), CLOSE, JST)
        current = stamp()
        if current < decide_from:
            return status("waiting_signal", target)
        if path.exists():
            decision = swing.read(path)
            if (
                decision["signal_date"],
                decision["target"],
                decision["due"],
                decision["strategy"],
            ) != (signal, target, due, STRATEGY):
                raise ValueError("frozen_signal_calendar_conflict")
        else:
            failure = None
            decision = None
            if current < deadline:
                cutoff = min(
                    current, datetime.combine(date.fromisoformat(signal), time(23, 59, 59), JST)
                )
                if (book / "STOP").exists() or (project / "data/swing-forward/STOP").exists():
                    decision = dict(
                        strategy=STRATEGY,
                        status="stopped",
                        signal_date=signal,
                        target=target,
                        due=due,
                        picks=[],
                        cutoff=cutoff.isoformat(),
                    )
                else:
                    try:
                        decision = prepare(project, signal, target, due, cutoff)
                        decision["decision_origin"] = "evening_inputs"
                    except INPUT_ERRORS as exc:
                        failure = str(exc)
                        retry_at = stamp()
                        if current.date().isoformat() == target and cutoff < retry_at < deadline:
                            try:
                                decision = prepare(project, signal, target, due, retry_at)
                                decision["decision_origin"] = "morning_recovery"
                                decision["original_cutoff"] = cutoff.isoformat()
                            except INPUT_ERRORS as retry_exc:
                                failure = str(retry_exc)
                if decision is None and stamp() < deadline:
                    return status("waiting_inputs", target, issue=failure)
            frozen_at = stamp()
            if frozen_at >= deadline:
                # A late runner may manage existing holdings, but never selects retrospective buys.
                decision = dict(
                    strategy=STRATEGY,
                    status="missed_preopen",
                    signal_date=signal,
                    target=target,
                    due=due,
                    picks=[],
                    issue=failure,
                )
            decision.update(recorded_at=frozen_at.isoformat(), calendar_sources=calendar_refs)
            swing.write(path, decision)
        last_decision = signal
        notice = preopen_stop(project, book, folder, decision, deadline, stamp)
        effective = dict(decision, picks=[], status="stopped_preopen") if notice else decision
        if stamp() < mark_from:
            return status("waiting_close", target, effective)
        try:
            close = settle(project, state, decision, stamp(), notice)
        except INPUT_ERRORS as exc:
            return status("waiting_close_inputs", target, effective, str(exc))
        swing.write(folder / "close.json", close)
        state = close["account"]
        signal = target
