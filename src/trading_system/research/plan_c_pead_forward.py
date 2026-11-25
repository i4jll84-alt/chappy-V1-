"""Prospective paper-only GRID_DIP_H60_ATR40 account; no broker interfaces."""

import math
import sqlite3
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from zoneinfo import ZoneInfo

import duckdb

from . import swing_forward as files
from .pead_source import PEAD_SOURCE_COMMIT, load_pead_module

VERSION = "pead-dip3-atr4-h60-v1"
STRATEGY = "GRID_DIP_H60_ATR40"
JST = ZoneInfo("Asia/Tokyo")
INPUT_ERRORS = (FileNotFoundError, ValueError, sqlite3.Error, duckdb.Error)


def at(day, hour, minute=0):
    return datetime.combine(date.fromisoformat(day), time(hour, minute), JST)


def calendar(project, signal, now):
    feed = load_pead_module("daily_feed")
    return feed.load_trading_days(project, signal, now=now)


def load_snapshot(project, day, now, *, execution=False):
    feed = load_pead_module("daily_feed")
    try:
        feed.ensure_daily_cache(project, day, now=now)
    except (OSError, ValueError, sqlite3.Error, duckdb.Error):
        # A refresh failure does not invalidate already verified cache evidence.
        pass
    loader = feed.load_execution_snapshot if execution else feed.load_daily_snapshot
    return loader(project, day, now=now)


def stopped(project, book):
    return any(p.is_file() for p in (book / "STOP", project / "data/plan-forward/STOP"))


def operate(book, capital, state, callback):
    """Native mutations occur only in scratch space; immutable session files are authoritative."""
    ops = load_pead_module("daily_ops")
    book.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".pead-", dir=book) as directory:
        root = Path(directory)
        cfg = ops.LiveConfig(
            initial_capital=capital,
            state_path=str(root / "state.json"),
            orders_dir=str(root / "orders"),
        )
        op = ops.DailyPeadOperator(cfg)
        if state is not None:
            files.write(op.path, state)
        callback(op)
        return op.status()


def initial(book, capital):
    return operate(book, capital, None, lambda op: None)


def account(state, capital, last_mark=None, peak=None, max_dd=0):
    cash = state["settled_cash"] + sum(x["amount"] for x in state["unsettled"])
    positions = {}
    for p in state["positions"]:
        cost = p["entry_price"] * p["shares"] * 1.0005
        positions[p["code"]] = dict(p, cost=cost, market_value=p["last_mark"] * p["shares"])
    nav = cash + sum(p["market_value"] for p in positions.values())
    high = max(peak or capital, nav)
    unrealized = sum(p["market_value"] - p["cost"] for p in positions.values())
    return dict(
        initial_capital=capital,
        cash=cash,
        available_cash=state["settled_cash"],
        nav=nav,
        positions=positions,
        last_mark=last_mark,
        holdings_date=last_mark,
        realized=nav - capital - unrealized,
        unrealized=unrealized,
        fees=state.get("paper_total_fees", 0),
        entitlement_review=state.get("paper_entitlement_review", []),
        valuation_complete=not state.get("paper_entitlement_review"),
        review_status="needs_entitlement_review" if state.get("paper_entitlement_review") else None,
        peak_nav=high,
        max_drawdown_pct=min(max_dd, (nav / high - 1) * 100),
    )


def safe_snapshot(signal, previous):
    snapshot = (
        deepcopy(previous)
        if previous
        else dict(
            date=signal, topix_close=None, topix_sma20=None, candidates=[], bars=[], alerts=[]
        )
    )
    snapshot.update(date=signal, candidates=[])
    if not isinstance(snapshot["bars"], dict):
        snapshot["bars"] = {}
    return snapshot


def freeze(project, book, native, previous_snapshot, signal, target, sessions, clock, capital):
    cutoff = clock()
    deadline = at(target, 9)
    late = cutoff >= deadline
    snapshot = safe_snapshot(signal, previous_snapshot)
    coverage = False
    bullish = False
    ranked = []
    if not late:
        snapshot, source_days = load_snapshot(project, signal, cutoff)
        if snapshot["date"] != signal or source_days != sessions:
            raise ValueError("pead_snapshot_calendar_mismatch")
        coverage = snapshot.get("audit", {}).get("financial_complete", False)
        ranked = list(snapshot["candidates"])
        bullish = snapshot["topix_sma20"] is not None and (
            snapshot["topix_close"] > snapshot["topix_sma20"]
        )
        if bullish and not coverage:
            snapshot = dict(snapshot, candidates=[])
    blocked = late or stopped(project, book)
    if blocked:
        snapshot = dict(snapshot, candidates=[])
    if late:
        # No preopen decision exists: carry protective stops and previously scheduled exits,
        # never infer a discretionary order after its auction.
        planned = deepcopy(native)
        planned.update(last_processed_date=signal, pending_target=target, pending_buys=[])
    else:
        planned = operate(
            book,
            capital,
            native,
            lambda op: op.run_eod_plan(signal, snapshot, sessions),
        )
    recorded = clock()
    if recorded >= deadline:
        late = True
        planned["pending_buys"] = []
        prior_sells = {p["code"]: p["pending_sell_reason"] for p in native["positions"]}
        for p in planned["positions"]:
            p["pending_sell_reason"] = prior_sells.get(p["code"])
    if blocked:
        planned["pending_buys"] = []
    picks = deepcopy(planned["pending_buys"])
    sells = [
        dict(code=p["code"], shares=p["shares"] // 100 * 100, reason=p["pending_sell_reason"])
        for p in planned["positions"]
        if p["pending_sell_reason"] and p["shares"] >= 100
    ]
    return dict(
        version=VERSION,
        strategy=STRATEGY,
        source_commit=PEAD_SOURCE_COMMIT,
        signal_date=signal,
        target=target,
        cutoff=cutoff.isoformat(),
        recorded_at=recorded.isoformat(),
        decision_origin="morning_recovery"
        if recorded.date().isoformat() == target
        else "evening_inputs",
        state=(
            "missed_preopen"
            if late
            else "stopped"
            if blocked
            else "exits_only"
            if bullish and not coverage
            else "ready"
        ),
        allow_entries=not late and not blocked and coverage and bullish,
        coverage_ok=coverage,
        topix_bullish=bullish if not late else None,
        regime_reason=(
            "unavailable_preopen"
            if late
            else "topix_above_sma20"
            if bullish
            else "topix_below_or_equal_sma20"
        ),
        ranked=ranked,
        no_buy_reason=(
            "missed_preopen"
            if late
            else "stopped"
            if blocked
            else "financial_coverage_unverified"
            if bullish and not coverage
            else "topix_below_or_equal_sma20"
            if not bullish
            else "no_eligible_or_sized_candidates"
            if not picks
            else None
        ),
        picks=picks,
        plan=dict(buys=picks, sells=sells),
        operator_state=planned,
        snapshot=dict(
            snapshot,
            bars={
                c: r
                for c, r in snapshot["bars"].items()
                if c in {p["code"] for p in planned["positions"] + planned["pending_buys"]}
            },
        ),
        inputs=snapshot.get("audit", {}),
        carried_sells={p["code"]: p["pending_sell_reason"] for p in native["positions"]},
        starting_account_sha256=files.signature(native),
    )


def publish_decision(path, decision, clock):
    # Selection, computation and disk staging must all finish before the auction deadline.
    staging = path.with_suffix(".pending")
    files.write(staging, decision)
    sealed = clock()
    if sealed < at(decision["target"], 9):
        staging.replace(path)
        return decision
    staging.unlink(missing_ok=True)
    decision = deepcopy(decision)
    decision.update(
        state="missed_preopen", allow_entries=False, picks=[], no_buy_reason="missed_preopen"
    )
    decision["plan"]["buys"] = []
    native = decision["operator_state"]
    native["pending_buys"] = []
    for p in native["positions"]:
        p["pending_sell_reason"] = decision["carried_sells"].get(p["code"])
    decision["plan"]["sells"] = [
        dict(code=p["code"], shares=p["shares"] // 100 * 100, reason=p["pending_sell_reason"])
        for p in native["positions"]
        if p["pending_sell_reason"] and p["shares"] >= 100
    ]
    decision["recorded_at"] = sealed.isoformat()
    files.write(path, decision)
    return decision


def bar(snapshot, code):
    row = snapshot["bars"].get(code)
    if (
        row
        and row.get("no_trade")
        and all(row.get(k) is None for k in ("open", "high", "low", "close"))
    ):
        return dict(row, no_trade=True)
    if not row or any(
        isinstance(row.get(k), bool)
        or not isinstance(row.get(k), (float, int))
        or not math.isfinite(row[k])
        or row[k] <= 0
        for k in ("open", "high", "low", "close")
    ):
        raise ValueError("pead_execution_bar_unverified:" + code)
    if (
        not row["low"]
        <= min(row["open"], row["close"])
        <= max(row["open"], row["close"])
        <= row["high"]
    ):
        raise ValueError("pead_execution_bar_invalid:" + code)
    return row


def settle(book, capital, decision, snapshot, sessions, notice):
    """Model the auction, then stops including entry day. All state commits together."""
    ops = load_pead_module("daily_ops")
    day = decision["target"]
    if snapshot["date"] != day:
        raise ValueError("pead_execution_date_mismatch")
    native = deepcopy(decision["operator_state"])
    if notice:
        native["pending_buys"] = []
    exchanges = snapshot.get("exchanges", [])
    canceled = {x["old_code"] for x in exchanges if x["effective"] == day}
    native["pending_buys"] = [p for p in native["pending_buys"] if p["code"] not in canceled]
    # Reuse the source ledger's verified share-exchange entitlement/price conversion.
    scratch = ops.DailyPeadOperator(
        ops.LiveConfig(
            state_path=str(book / ".unused-state"), orders_dir=str(book / ".unused-orders")
        )
    )
    scratch._exchanges(native, dict(date=day, bars=snapshot["bars"], exchanges=exchanges))
    relevant = {p["code"] for p in native["positions"] + native["pending_buys"]}
    # Reject missing bars for held or planned names rather than assume no stop/auction.
    bars = {c: bar(snapshot, c) for c in relevant}
    reviews = list(native.get("paper_entitlement_review", []))
    for p in native["positions"]:
        if p["entry_date"] < day and bars[p["code"]].get("spinoff"):
            note = dict(
                date=day,
                code=p["code"],
                kind="spinoff_entitlement",
                note="スピンオフ対価は未照合です。現金を仮計上していません。評価損益は未確定です。",
            )
            if note not in reviews:
                reviews.append(note)
    native["paper_entitlement_review"] = reviews
    units = {
        c: dict(
            factor=r.get("factor", 1),
            share_factor=r.get("share_factor", r.get("factor", 1)),
            spinoff=r.get("spinoff", False),
        )
        for c, r in bars.items()
    }
    buys, sells = {}, {}
    for p in native["pending_buys"]:
        row = bars[p["code"]]
        factor = row.get("factor", 1)
        cap = ops.round_down_to_tse_tick(p["limit_price"] * factor)
        if not row.get("no_trade") and row["open"] <= cap:
            price = min(float(Decimal(str(row["open"])) * Decimal("1.001")), cap)
            shares = (
                ops._scaled_shares(p["planned_shares"], row.get("share_factor", factor), p["code"])
                // 100
                * 100
            )
            if shares > 0:
                buys[p["code"]] = dict(price=price, shares=shares)
    for p in native["positions"]:
        if p["pending_sell_reason"] and p["shares"] >= 100:
            row = bars[p["code"]]
            qty = (
                ops._scaled_shares(
                    p["shares"], row.get("share_factor", row.get("factor", 1)), p["code"]
                )
                // 100
                * 100
            )
            if qty and not row.get("no_trade"):
                sells[p["code"]] = dict(price=row["open"] * 0.999, shares=qty)

    def execute(op):
        op.confirm_morning_executions(day, buys, sells, sessions, unit_actions=units)
        for p in list(op.status()["positions"]):
            row = bars[p["code"]]
            stop = p["stop_price"]
            quantity = p["shares"] // 100 * 100
            if not row.get("no_trade") and quantity and stop > 0 and row["low"] <= stop:
                price = min(row["open"], stop) * 0.999
                op.record_sell(
                    day,
                    p["code"],
                    price,
                    quantity,
                    sessions,
                    fill_id=f"paper-stop:{day}:{p['code']}",
                    reason="PAPER_HARD_STOP",
                    unit_actions=units,
                )

    settled = operate(book, capital, native, execute)
    for p in settled["positions"]:
        if not bars[p["code"]].get("no_trade"):
            p["last_mark"] = bars[p["code"]]["close"]
    settled["paper_total_fees"] = native.get("paper_total_fees", 0) + sum(
        f["fee"] for rows in settled["fills"].values() for f in rows if f["date"] == day
    )
    # Journal replay requires today's receipts only. Immutable earlier session files retain history.
    keys = {e["key"] for e in settled["daily_journal"]["events"]}
    for field in ("receipts", "fills"):
        settled[field] = {k: v for k, v in settled[field].items() if k in keys}
    settled["plans"] = {}
    settled["plan_unit_history"] = {}
    return settled


def advance(project: Path, book: Path, *, start: str, capital: int, now: datetime):
    if now.utcoffset() is None:
        raise ValueError("timezone_required")
    if any(p.casefold().startswith("onedrive") for p in book.resolve().parts):
        raise ValueError("pead_requires_local_book")
    now = now.astimezone(JST)
    tick = monotonic()

    def clock():
        return now + timedelta(seconds=monotonic() - tick)

    seed = dict(
        version=VERSION,
        strategy=STRATEGY,
        start=start,
        initial_capital=capital,
        source_commit=PEAD_SOURCE_COMMIT,
    )
    path = book / "initial.json"
    if path.exists():
        if files.read(path) != seed:
            raise ValueError("pead_account_terms_changed")
    else:
        files.write(path, seed)
    native = initial(book, capital)
    state = account(native, capital)
    signal, previous_snapshot = start, None
    for path in sorted((book / "sessions").glob("????-??-??/close.json")):
        close = files.read(path)
        decision = files.read(path.with_name("decision.json"))
        if (
            close["version"] != VERSION
            or close["date"] != path.parent.name
            or close["decision_sha256"] != files.signature(decision)
            or close["previous_native_sha256"] != files.signature(native)
            or decision["starting_account_sha256"] != files.signature(native)
            or close["native_sha256"] != files.signature(close["operator_state"])
            or close["bars_sha256"] != files.signature(close["snapshot"])
            or close["account"]
            != account(
                close["operator_state"],
                capital,
                close["date"],
                state["peak_nav"],
                state["max_drawdown_pct"],
            )
            or decision["signal_date"] != signal
            or decision["target"] != close["date"]
        ):
            raise ValueError("pead_immutable_ledger_mismatch")
        stop_path = path.with_name("preopen_stop.json")
        if close["preopen_stop_sha256"] != (
            files.signature(files.read(stop_path)) if stop_path.exists() else None
        ):
            raise ValueError("pead_immutable_preopen_stop_mismatch")
        native, state, signal = close["operator_state"], close["account"], close["date"]
        previous_snapshot = close["snapshot"]

    def summary(status, target=None, decision=None, issue=None):
        d = decision or {}
        return dict(
            state,
            status="needs_review" if state.get("entitlement_review") else status,
            processing_status=status,
            next_signal=signal,
            next_entry=target,
            last_decision=d.get("signal_date"),
            decision_state=d.get("state"),
            decision_status=d.get("state"),
            decision_recorded_at=d.get("recorded_at"),
            decision_cutoff=d.get("cutoff"),
            decision_origin=d.get("decision_origin"),
            picks=d.get("picks", []),
            orders=len(d.get("picks", [])),
            candidates=len(d.get("ranked", [])),
            ranked=d.get("ranked", []),
            no_buy_reason=d.get("no_buy_reason"),
            regime_reason=d.get("regime_reason"),
            entry_decision_pending=d.get("state") == "exits_only",
            topix_bullish=d.get("topix_bullish"),
            allow_entries=d.get("allow_entries"),
            coverage_ok=d.get("coverage_ok"),
            issue=issue,
        )

    while True:
        if clock() < at(signal, 15, 30):
            return summary("waiting_signal")
        try:
            sessions = calendar(project, signal, clock())
            target = sessions[sessions.index(signal) + 1]
            if sessions.index(target) + 2 >= len(sessions):
                raise ValueError("pead_settlement_calendar_short")
        except (*INPUT_ERRORS, IndexError) as exc:
            return summary(
                "waiting_calendar",
                issue=str(exc) if str(exc).startswith("pead_") else type(exc).__name__,
            )
        folder = book / "sessions" / target
        path = folder / "decision.json"
        interim = folder / "exit-decision.json"
        if path.is_file():
            decision = files.read(path)
        elif clock() >= at(target, 9) and interim.is_file():
            decision = files.read(interim)
            if (
                decision["picks"]
                or decision["state"] != "exits_only"
                or datetime.fromisoformat(decision["recorded_at"]) >= at(target, 9)
            ):
                raise ValueError("pead_invalid_exit_evidence")
            # Copy the exact prospectively sealed exit evidence; never recompute after the auction.
            files.write(path, decision)
        else:
            try:
                decision = freeze(
                    project,
                    book,
                    native,
                    previous_snapshot,
                    signal,
                    target,
                    sessions,
                    clock,
                    capital,
                )
            except INPUT_ERRORS as exc:
                return summary(
                    "waiting_inputs",
                    target,
                    issue=str(exc) if str(exc).startswith("pead_") else type(exc).__name__,
                )
            if decision["state"] == "exits_only" and clock() < at(target, 9):
                if not interim.exists():
                    candidate = publish_decision(interim, decision, clock)
                    if candidate["state"] != "exits_only":
                        # Staging crossed the cutoff: only original carried exits survived.
                        interim.unlink(missing_ok=True)
                        files.write(path, candidate)
                    else:
                        return summary(
                            "waiting_inputs",
                            target,
                            candidate,
                            issue="pead_financial_coverage_unverified",
                        )
                else:
                    return summary(
                        "waiting_inputs",
                        target,
                        files.read(interim),
                        issue="pead_financial_coverage_unverified",
                    )
                decision = files.read(path)
            else:
                decision = publish_decision(path, decision, clock)
        if (
            decision["version"] != VERSION
            or decision["strategy"] != STRATEGY
            or decision["signal_date"] != signal
            or decision["target"] != target
            or decision["starting_account_sha256"] != files.signature(native)
            or decision["picks"] != decision["plan"]["buys"]
            or decision["picks"] != decision["operator_state"]["pending_buys"]
            or (
                decision["picks"]
                and (
                    not decision["allow_entries"]
                    or datetime.fromisoformat(decision["recorded_at"]) >= at(target, 9)
                )
            )
        ):
            raise ValueError("pead_frozen_decision_mismatch")
        notice_path = folder / "preopen_stop.json"
        if clock() < at(target, 9):
            if stopped(project, book) and not notice_path.exists():
                files.write(
                    notice_path,
                    dict(
                        recorded_at=clock().isoformat(), decision_sha256=files.signature(decision)
                    ),
                )
        notice = files.read(notice_path) if notice_path.exists() else None
        if notice and (
            notice["decision_sha256"] != files.signature(decision)
            or not datetime.fromisoformat(decision["recorded_at"])
            <= datetime.fromisoformat(notice["recorded_at"])
            < at(target, 9)
        ):
            raise ValueError("pead_preopen_stop_mismatch")
        effective = (
            dict(decision, picks=[], state="stopped_preopen", allow_entries=False)
            if notice
            else decision
        )
        if clock() < at(target, 15, 30):
            return summary(
                "waiting_for_open" if clock() < at(target, 9) else "waiting_close",
                target,
                effective,
            )
        try:
            snapshot, execution_sessions = load_snapshot(project, target, clock(), execution=True)
            if execution_sessions != sessions:
                raise ValueError("pead_execution_calendar_changed")
            settled = settle(book, capital, decision, snapshot, sessions, notice)
        except INPUT_ERRORS as exc:
            return summary(
                "waiting_close_inputs",
                target,
                effective,
                str(exc) if str(exc).startswith("pead_") else type(exc).__name__,
            )
        state = account(settled, capital, target, state["peak_nav"], state["max_drawdown_pct"])
        retained = {
            p["code"]
            for p in (
                decision["operator_state"]["positions"]
                + decision["operator_state"]["pending_buys"]
                + settled["positions"]
            )
        }
        retained.update(
            x["new_code"] for x in snapshot.get("exchanges", []) if x["old_code"] in retained
        )
        snapshot = dict(snapshot, bars={c: r for c, r in snapshot["bars"].items() if c in retained})
        files.write(
            folder / "close.json",
            dict(
                version=VERSION,
                date=target,
                recorded_at=clock().isoformat(),
                decision_sha256=files.signature(decision),
                preopen_stop_sha256=files.signature(notice) if notice else None,
                previous_native_sha256=files.signature(native),
                native_sha256=files.signature(settled),
                operator_state=settled,
                account=state,
                snapshot=snapshot,
                bars_sha256=files.signature(snapshot),
            ),
        )
        native, signal, previous_snapshot = settled, target, snapshot
