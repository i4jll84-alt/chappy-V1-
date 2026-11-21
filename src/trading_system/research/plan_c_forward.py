"""Prospective paper adapter for the fixed Plan C pullback strategy."""

import json
import math
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from . import a0_5_inputs, c1_execution, c1_inputs
from . import swing_forward as files

VERSION = "c1-pullback-guarded-v1"
JST = ZoneInfo("Asia/Tokyo")
INPUT_ERRORS = (FileNotFoundError, ValueError, sqlite3.Error, duckdb.Error)


def read_settings(project):
    path = project / "config/c1-pullback.json"
    settings = {
        "enable_earnings_guard": True,
        "enable_vol_circuit_breaker": False,
        "vol_threshold": 30.0,
        "vol_source": "NIKKEI_VI",
    }
    if path.is_file():
        overrides = json.loads(path.read_bytes())
        if set(overrides) - set(settings):
            raise ValueError("unknown_c1_setting")
        settings.update(overrides)
    if (
        settings["enable_earnings_guard"] is not True
        or type(settings["enable_vol_circuit_breaker"]) is not bool
        or settings["vol_source"] not in {"NIKKEI_VI", "VIX"}
        or type(settings["vol_threshold"]) not in {int, float}
        or not math.isfinite(settings["vol_threshold"])
        or settings["vol_threshold"] <= 0
    ):
        raise ValueError("invalid_c1_settings")
    return settings


def calendar(project, signal, cutoff):
    refs = []
    cal = a0_5_inputs.calendar(project, pd.Timestamp(signal), cutoff, refs)
    return [d.date().isoformat() for d in cal], refs


def at(day, hour, minute=0):
    return datetime.combine(date.fromisoformat(day), time(hour, minute), JST)


def stopped(project, book):
    return any(path.is_file() for path in (book / "STOP", project / "data/plan-forward/STOP"))


def publish_decision(path, decision, prior, clock):
    """Seal the complete decision on disk before admitting any pre-open buy."""
    if decision["plan"]["buys"]:
        staging = path.with_suffix(".pending")
        files.write(staging, decision)
        sealed = clock()
        if sealed < at(decision["target"], 9):
            staging.replace(path)
            return decision
        staging.unlink(missing_ok=True)
        decision = prior or dict(
            decision,
            state="missed_preopen",
            ranked=[],
            allow_entries=False,
            plan=dict(decision["plan"], buys=[]),
            recorded_at=sealed.isoformat(),
        )
    files.write(path, decision)
    return decision


def freeze(project, book, account, signal, target, sessions, sources, clock, settings):
    deadline = at(target, 9)
    cutoff = clock()
    stamp = min(cutoff, deadline - timedelta(microseconds=1))
    data = {
        "ranked": [],
        "marks": {},
        "earnings": {},
        "allow_entries": False,
        "coverage_ok": False,
        "reasons": [],
        "counts": {},
        "refs": [],
    }
    if cutoff < deadline or account["positions"]:
        try:
            data = c1_inputs.load_day(project, signal, stamp, **settings)
        except INPUT_ERRORS as exc:
            if clock() < deadline:
                raise
            data["reasons"].append(type(exc).__name__)
    paused = stopped(project, book)
    inputs_ready = data["coverage_ok"] and data.get("earnings_verified") is True
    enabled = inputs_ready and data["allow_entries"] and not paused
    state = "ready" if inputs_ready else "insufficient_data"
    if paused:
        state = "stopped"
    plan = c1_execution.freeze_plan(
        account,
        signal,
        target,
        data["ranked"],
        data["marks"],
        data["earnings"],
        sessions,
        allow_entries=enabled,
        enable_earnings_guard=True,
    )
    recorded = clock()
    if recorded >= deadline:
        # Time exits and protective stops still run. Never backdate an entry order.
        state = "missed_preopen"
        plan = c1_execution.freeze_plan(
            account,
            signal,
            target,
            [],
            data["marks"],
            data["earnings"],
            sessions,
            allow_entries=False,
            enable_earnings_guard=True,
        )
    return {
        "version": VERSION,
        "signal_date": signal,
        "target": target,
        "state": state,
        "cutoff": stamp.isoformat(),
        "recorded_at": recorded.isoformat(),
        "decision_origin": "morning_recovery"
        if recorded.date().isoformat() == target
        else "evening_inputs",
        "settings": settings,
        "counts": data["counts"],
        "reasons": data["reasons"],
        "coverage_ok": data["coverage_ok"],
        "earnings_verified": data.get("earnings_verified"),
        "allow_entries": enabled and state != "missed_preopen",
        "ranked": data["ranked"] if state != "missed_preopen" else [],
        "inputs": data["refs"],
        "calendar_refs": sources,
        "plan": plan,
        "starting_account_sha256": files.signature(account),
    }


def advance(project: Path, book: Path, *, start: str, capital: int, now: datetime) -> dict:
    if now.utcoffset() is None:
        raise ValueError("timezone_required")
    now = now.astimezone(JST)
    tick = monotonic()

    def clock():
        return now + timedelta(seconds=monotonic() - tick)

    seed = {"strategy": VERSION, "start": start, "initial_capital": capital}
    initial = book / "initial.json"
    if initial.exists():
        if files.read(initial) != seed:
            raise ValueError("c1_account_terms_changed")
    else:
        files.write(initial, seed)
    state = c1_execution.initial_account(capital)
    signal = start
    closes = sorted((book / "sessions").glob("????-??-??/close.json"))
    if closes:
        last = closes[-1]
        saved = files.read(last)
        opened = files.read(last.with_name("open.json"))
        decision = files.read(last.with_name("decision.json"))
        if (
            saved["open_sha256"] != files.signature(opened)
            or opened["decision_sha256"] != files.signature(decision)
            or saved["date"] != last.parent.name
            or saved["version"] != VERSION
        ):
            raise ValueError("c1_ledger_mismatch")
        state = saved["account"]
        signal = saved["date"]

    def summary(status, target=None, decision=None, reason=None):
        d = decision or {}
        return {
            "status": status,
            "initial_capital": capital,
            "cash": state["cash"],
            "available_cash": c1_execution.available_cash(state, clock().date().isoformat()),
            "nav": state["equity"][-1]["nav"] if state["equity"] else state["capital"],
            "positions": state["positions"],
            "last_mark": state.get("last_date"),
            "holdings_date": state.get("open_date") or state.get("last_date"),
            "next_entry": target,
            "last_decision": d.get("signal_date"),
            "decision_state": d.get("state"),
            "coverage_counts": d.get("counts"),
            "allow_entries": d.get("allow_entries"),
            "decision_recorded_at": d.get("recorded_at"),
            "decision_cutoff": d.get("cutoff"),
            "decision_origin": d.get("decision_origin"),
            "candidates": len(d["ranked"]) if d else None,
            "orders": len(d["plan"]["buys"]) if d else None,
            "reason": reason or "、".join(map(str, d.get("reasons", []))) or None,
        }

    settings = read_settings(project)
    while True:
        if clock() < at(signal, 15, 30):
            return summary("waiting_signal")
        try:
            sessions, sources = calendar(project, signal, clock())
            target = sessions[sessions.index(signal) + 1]
        except (FileNotFoundError, ValueError, sqlite3.Error, duckdb.Error, IndexError) as exc:
            return summary("waiting_calendar", reason=type(exc).__name__)
        folder = book / "sessions" / target
        decision_file = folder / "decision.json"
        if decision_file.is_file():
            decision = files.read(decision_file)
        else:
            attempts = sorted((folder / "attempts").glob("*.json"))
            prior = files.read(attempts[-1]) if attempts else None
            if prior is not None and clock() >= at(target, 9):
                decision = prior
            else:
                try:
                    decision = freeze(
                        project, book, state, signal, target, sessions, sources, clock, settings
                    )
                except INPUT_ERRORS as exc:
                    return summary("waiting_inputs", target, prior, reason=str(exc))
                if decision["state"] == "insufficient_data":
                    stamp = datetime.fromisoformat(decision["recorded_at"])
                    files.write(
                        folder / "attempts" / stamp.strftime("%Y%m%dT%H%M%S%f.json"), decision
                    )
                    return summary("waiting_inputs", target, decision)
                if prior is not None and decision["state"] == "missed_preopen":
                    decision = prior
            decision = publish_decision(decision_file, decision, prior, clock)
        if (
            decision["version"] != VERSION
            or decision["signal_date"] != signal
            or decision["target"] != target
            or decision["starting_account_sha256"] != files.signature(state)
        ):
            raise ValueError("c1_frozen_decision_mismatch")
        notice = folder / "preopen_stop.json"
        if clock() < at(target, 9):
            if stopped(project, book) and not notice.exists():
                files.write(
                    notice,
                    {
                        "recorded_at": clock().isoformat(),
                        "decision_sha256": files.signature(decision),
                    },
                )
            return summary("waiting_for_open", target, decision)
        plan = decision["plan"]
        if notice.is_file():
            evidence = files.read(notice)
            if evidence["decision_sha256"] != files.signature(decision) or datetime.fromisoformat(
                evidence["recorded_at"]
            ) >= at(target, 9):
                raise ValueError("c1_preopen_stop_mismatch")
            plan = dict(plan, buys=[])
        opening = folder / "open.json"
        bars = None
        if opening.exists():
            opened = files.read(opening)
        else:
            try:
                bars = c1_inputs.load_bars(project, target, clock())
            except INPUT_ERRORS as exc:
                return summary("waiting_close_inputs", target, decision, str(exc))
            try:
                new_state = c1_execution.execute_open(state, target, plan, bars, sessions)
            except c1_execution.C1UnverifiedBar as exc:
                return summary("waiting_close_inputs", target, decision, str(exc))
            opened = {
                "version": VERSION,
                "date": target,
                "recorded_at": clock().isoformat(),
                "decision_sha256": files.signature(decision),
                "account": new_state,
            }
            files.write(opening, opened)
        if opened["decision_sha256"] != files.signature(decision):
            raise ValueError("c1_frozen_opening_mismatch")
        state = opened["account"]
        if clock() < at(target, 15, 30):
            return summary("waiting_close", target, decision)
        try:
            if bars is None:
                bars = c1_inputs.load_bars(project, target, clock())
        except INPUT_ERRORS as exc:
            return summary("waiting_close_inputs", target, decision, str(exc))
        try:
            state = c1_execution.execute_close(state, target, bars, sessions)
        except c1_execution.C1UnverifiedBar as exc:
            return summary("waiting_close_inputs", target, decision, str(exc))
        files.write(
            folder / "close.json",
            {
                "version": VERSION,
                "date": target,
                "recorded_at": clock().isoformat(),
                "open_sha256": files.signature(opened),
                "bars_sha256": files.signature(bars),
                "account": state,
            },
        )
        signal = target
