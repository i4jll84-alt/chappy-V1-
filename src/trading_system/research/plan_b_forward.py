"""Forward comparison view of an existing, isolated A0-5 paper account."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, time
from pathlib import Path

import pandas as pd

from ..storage.store import atomic_write
from . import a0_5 as native
from . import a0_5_book as ledger


def _revision_digest(db_path, revision):
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT body FROM revisions WHERE revision=?", (revision,)).fetchone()
    if row is None:
        raise ValueError("Bプラン開始時のA0-5台帳の版を確認できません")
    return hashlib.sha256(row[0].encode("utf-8")).hexdigest()


def _checkpoint(path, native_db, state, start, capital, now, calendar):
    if path.is_file():
        bound = json.loads(path.read_bytes())
        if (
            bound["start"] != start
            or bound["initial_capital"] != capital
            or bound["native_revision"] > state["revision"]
            or bound["native_revision_sha256"]
            != _revision_digest(native_db, bound["native_revision"])
        ):
            raise ValueError("Bプランの開始記録と参照先のA0-5台帳が一致しません")
        return bound
    today = now.date().isoformat()
    if today > start or (today == start and now.time() >= time(15, 30)):
        raise ValueError("Bプランの初回登録は開始日の引け前に必要です")
    first = pd.Timestamp(start)
    if first not in calendar or calendar[-1] <= first:
        raise ValueError("Bプランの開始立会日と翌立会日を確認できません")
    pending = any(
        p["state"] == "ready" and p["orders"] and p["order_date"] not in state["openings"]
        for p in state["plans"].values()
    )
    if (
        state["cash"] != capital
        or state["positions"]
        or state["settlements"]
        or pending
        or any(day >= start for day in state["plans"])
        or (state["last_open"] and state["last_open"] >= start)
    ):
        raise ValueError("A0-5既存口座に既存の建玉・受渡・注文・判定または資金差額があります")
    bound = {
        "initial_capital": capital,
        "start": start,
        "first_entry": calendar[calendar.get_loc(first) + 1].date().isoformat(),
        "registered_at": now.isoformat(),
        "native_revision": state["revision"],
        "native_revision_sha256": _revision_digest(native_db, state["revision"]),
        "journal_offset": len(state["journal"]),
    }
    atomic_write(path, json.dumps(bound, indent=2, ensure_ascii=False).encode())
    return bound


def advance(project: Path, book: Path, *, start: str, capital: int, now: datetime) -> dict:
    """Read paper activity since the registered start under the caller's account lock."""
    if now.tzinfo is None:
        raise ValueError("Bプランの集計時刻にはタイムゾーンが必要です")
    now = now.astimezone(native.JST)
    today = now.date().isoformat()
    root = native.root_for(project, capital)
    native_db = root / "ledger.sqlite"
    if not native_db.is_file():
        raise FileNotFoundError("Bプランが参照するA0-5口座の台帳がありません")
    state = ledger.load(root, capital)
    calendar = native.inputs.calendar(project, pd.Timestamp(today), now, [])
    bound = _checkpoint(book / "baseline.json", native_db, state, start, capital, now, calendar)
    journal = state["journal"][bound["journal_offset"] :]
    if (
        len(state["journal"]) < bound["journal_offset"]
        or any(row["date"] <= start or row["date"] > today for row in journal)
        or any(day > today for day in state["plans"])
    ):
        raise ValueError("Bプランの開始日または集計日とA0-5の取引日が一致しません")
    decisions = [state["plans"][d] for d in sorted(state["plans"]) if d >= start]
    last = decisions[-1] if decisions else {}
    ready = [r for r in decisions if r["state"] == "ready"]
    mark = ready[-1] if ready else {}
    complete = [
        d.date().isoformat()
        for d in calendar
        if d.date() < now.date() or (d.date() == now.date() and now.time() >= time(15, 30))
    ]
    expected = complete[-1] if complete and complete[-1] >= start else None
    if native.stopped(project):
        status = "stopped"
    elif not native.enabled(project):
        status = "disabled"
    elif expected is None:
        status = "waiting_for_first_close"
    elif last.get("date") != expected:
        status = "awaiting_native_decision" if expected == today else "missing_native_decision"
    else:
        status = last["state"]
    next_entry = last.get("order_date") if last else bound["first_entry"]
    if next_entry and next_entry < today:
        next_entry = None
    return {
        "status": status,
        "initial_capital": capital,
        "cash": state["cash"],
        "positions": len(state["positions"]),
        "nav": mark.get("equity", capital),
        "last_mark": mark.get("date"),
        "last_decision": last.get("date"),
        "decision_state": last.get("state"),
        "reason": last.get("reason") if status == "blocked" else None,
        "next_entry": next_entry,
        "first_entry": bound["first_entry"],
        "candidates": len(last["candidates"]) if "candidates" in last else None,
        "orders": len(last["orders"]) if last.get("state") == "ready" else None,
        "fills": sum(r["status"] == "仮想約定" for r in journal),
        "native_revision": state["revision"],
        "native_book": str(root.relative_to(project).as_posix()),
    }
