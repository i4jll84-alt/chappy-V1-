"""Prospective V21 paper account. Only previously frozen instructions can create simulated fills."""

import json
import math
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import pandas as pd

from . import v21_runtime as runtime
from .daytrade_universe import dated_inputs
from .plan_a_forward import preopen_stop
from .swing_forward import read, signature, write


def at(day, clock):
    return datetime.fromisoformat(f"{day}T{clock}:00+09:00")


def snapshot(native, capital, marks, realized, day):
    held = {}
    for pos in native["positions"]:
        code = pos["code5"]
        value = pos["shares"] * marks[code]
        held[code] = dict(
            name=pos["name"],
            shares=pos["shares"],
            entry_date=pos["entry_date"],
            entry_price=pos["entry_price"],
            cost=pos["total_cost"],
            market_value=value,
            stop_price=pos["stop_price"],
        )
    nav = native["cash"] + sum(p["market_value"] for p in held.values())
    unrealized = sum(p["market_value"] - p["cost"] for p in held.values())
    if abs(nav - capital - realized - unrealized) > 0.02:
        raise ValueError("v21_cash_and_position_reconciliation")
    return dict(
        initial_capital=capital,
        cash=native["cash"],
        positions=held,
        nav=nav,
        realized=realized,
        unrealized=unrealized,
        last_mark=day,
    )


def prepare(project, scratch, prev, capital, window, target, now, stamp, *, entry_profile=None):
    signal = window[-1]
    deadline = at(target, "09:00")
    missed = dict(
        native=prev["native"],
        orders=[],
        credit_ready=False,
        price_and_master_sources=[],
        jsf_sources=[],
    )
    payload = missed
    if now < deadline:
        entries = (
            entry_profile(project, prev["account"], window, target, now) if entry_profile else None
        )
        payload = runtime.morning(
            project,
            scratch,
            prev["native"],
            capital,
            window,
            target,
            now,
            at(target, "07:45"),
            **({"entries": entries} if entries else {}),
        )
    recorded = stamp()
    if recorded >= deadline:
        payload = missed
        status = "missed_preopen"
    else:
        status = (
            "ready"
            if payload["credit_ready"] and not payload.get("no_buy_reason")
            else "exits_only"
        )
    picks = []
    for row in payload["orders"]:
        if row["区分"].startswith("買い#"):
            if status != "ready":
                raise ValueError("v21_unverified_buy")
            picks.append(
                dict(
                    code=row["コード5桁"],
                    name=row["銘柄名"],
                    shares=int(row["株数"]),
                    limit_price=float(row["指値または逆指値"]),
                    prev_close=float(row["基準価格"]),
                    rank=row.get("rank", len(picks) + 1),
                    **({"sector33_code": row["sector33_code"]} if entry_profile else {}),
                )
            )
    return dict(
        strategy=entry_profile.version if entry_profile else runtime.VERSION,
        target=target,
        signal_date=signal,
        cutoff=now.isoformat(),
        recorded_at=recorded.isoformat(),
        previous_sha256=signature(prev),
        picks=picks,
        status=status,
        **payload,
    )


def simulate(project, scratch, decision, capital, bars, previous, stopped):
    day = decision["target"]
    runner = runtime.native_runner(project, scratch, capital, decision["native"])
    positions = {p["code5"]: p for p in decision["native"]["positions"]}
    pending = {
        o["code5"]: o for o in decision["native"]["pending_orders"] if o["target_trade_date"] == day
    }
    rows = []
    entries = {c: (p["entry_date"], p["entry_price"]) for c, p in positions.items()}
    marks = {}

    def bar_for(code):
        b = bars.get(code)
        factor = decision.get("split_factors", {}).get(code, 1)
        if (
            not b
            or b.get("AdjFactor") is None
            or code in decision.get("blocked_splits", [])
            or not math.isclose(float(b["AdjFactor"]), factor, rel_tol=1e-7)
        ):
            raise ValueError("v21_corporate_action_or_missing_bar_review")
        return b

    def price(b, key):
        val = b.get(key)
        if val is None or not math.isfinite(float(val)) or float(val) <= 0:
            raise ValueError("v21_execution_price_unavailable")
        return float(val)

    def fill(code, side, px, qty, clock, order_id=""):
        seq = len(rows) + 1
        rows.append(
            dict(
                exec_id=f"PAPER-{day}-{seq}",
                exec_date=day,
                code=code,
                side=side,
                price=px,
                shares=qty,
                exec_time=clock,
                seq=seq,
                order_id=order_id,
            )
        )

    for row in decision["orders"]:
        label = row["区分"]
        code = row["コード5桁"]
        if label.startswith("買い#") and stopped:
            continue
        if not (label.startswith("買い#") or label in ("売り(成行・寄付)", "保有継続(逆指値設定)")):
            continue
        b = bar_for(code)
        if all(b.get(k) is None for k in ("O", "H", "L", "C")) and code not in positions:
            continue
        opening = price(b, "O")
        qty = int(row["株数"])
        if label.startswith("買い#"):
            limit = float(row["指値または逆指値"])
            if opening > limit:
                continue
            paid = min(limit, round(opening * 1.001, 4))
            fill(code, "BUY", paid, qty, "09:00:00", row["注文ID"])
            entries[code] = (day, paid)
            stop = round(paid - runner.cfg.atr_mult * pending[code]["selection_atr14"], 1)
            if price(b, "L") <= stop and stop > 0:
                fill(code, "SELL", round(stop * 0.999, 4), qty, "15:29:59")
        elif label == "売り(成行・寄付)":
            fill(code, "SELL", round(opening * 0.999, 4), qty, "09:00:00")
        else:
            stop = float(row["指値または逆指値"])
            if price(b, "L") <= stop:
                fill(
                    code,
                    "SELL",
                    round(min(opening, stop) * 0.999, 4),
                    qty,
                    "09:00:00" if opening <= stop else "15:29:59",
                )
    rows.sort(key=lambda row: row["exec_time"])
    for seq, row in enumerate(rows, 1):
        row["seq"] = seq
    with redirect_stdout(StringIO()):
        if rows:
            trades_file = scratch / "modeled.csv"
            pd.DataFrame(rows).to_csv(trades_file, index=False)
            runner.sync_after_open(day, trades_file)
        runner.cancel_pending_orders(day, cancel_time_str="15:30:00", cancel_date_str=day)
    native = json.loads(runner.cfg.state_path.read_bytes())
    expected = {r["exec_id"] for r in rows}
    ledger = [r for r in native["trade_ledger"] if r.get("exec_id") in expected]
    if native["cash_audit_hold"] or len(ledger) != len(rows):
        raise ValueError("v21_modeled_execution_held")
    for pos in native["positions"]:
        code = pos["code5"]
        marks[code] = price(bar_for(code), "C")
    trades, fees = [], []
    for item in ledger:
        qty, px = item["shares"], item["price"]
        fee = runner._calc_fee(qty * px)
        fees.append(dict(date=day, fee=fee))
        if item["side"] == "SELL":
            c = item["code5"]
            entry, entry_px = entries[c]
            trades.append(
                dict(
                    code=c[:4],
                    source_code=c,
                    entry_date=entry,
                    exit_date=day,
                    shares=qty,
                    entry_price=entry_px,
                    exit_price=px,
                    net=item["realized_pnl"],
                    fees=round(fee + item["allocated_cost"] - qty * entry_px, 4),
                )
            )
    realized = previous["account"]["realized"] + sum(t["net"] for t in trades)
    account = snapshot(native, capital, marks, realized, day)
    sectors = {
        p["code5"]: p["sector33_code"]
        for p in decision["native"]["positions"]
        if "sector33_code" in p
    }
    sectors.update(
        {p["code"]: p["sector33_code"] for p in decision.get("picks", []) if "sector33_code" in p}
    )
    for pos in native["positions"]:
        if pos["code5"] in sectors:
            pos["sector33_code"] = sectors[pos["code5"]]
            account["positions"][pos["code5"]]["sector33_code"] = sectors[pos["code5"]]
    return dict(native=native, account=account, trades=trades, fees=fees, modeled_executions=rows)


def advance(project, book, *, start, capital, now, entry_profile=None):
    version = entry_profile.version if entry_profile else runtime.VERSION
    elapsed = monotonic()

    def stamp():
        return now + timedelta(seconds=monotonic() - elapsed)

    path = book / "initial.json"
    with TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        if not path.exists():
            native = runtime.initial_native(project, scratch, capital)
            write(
                path,
                dict(
                    native=native,
                    account=snapshot(native, capital, {}, 0, start),
                    strategy=version,
                ),
            )
        prev = read(path)
        if prev["strategy"] != version or prev["account"]["initial_capital"] != capital:
            raise ValueError("v21_account_changed")
        marks = sorted((book / "sessions").glob("*/close.json"))
        if marks:
            prior = read(marks[-2]) if len(marks) > 1 else prev
            prev = read(marks[-1])
            if prev["previous_sha256"] != signature(prior) or prev["decision_sha256"] != signature(
                read(marks[-1].with_name("decision.json"))
            ):
                raise ValueError("v21_close_chain_changed")
        while True:
            signal = prev["account"]["last_mark"]
            summary = dict(**prev["account"], next_signal=signal)
            if now < at(signal, "15:30"):
                return dict(**summary, status="waiting_signal")
            try:
                window, target = runtime.session_window(project, signal, now)
            except (FileNotFoundError, ValueError, IndexError):
                return dict(**summary, status="waiting_calendar")
            folder = book / "sessions" / target
            decision_path = folder / "decision.json"
            summary["next_entry"] = target
            if not decision_path.is_file():
                try:
                    decision = prepare(
                        project,
                        scratch,
                        prev,
                        capital,
                        window,
                        target,
                        stamp(),
                        stamp,
                        entry_profile=entry_profile,
                    )
                except (FileNotFoundError, ValueError) as exc:
                    return dict(**summary, status="waiting_inputs", issue=str(exc)[:150])
                write(decision_path, decision)
            decision = read(decision_path)
            if (
                decision["target"] != target
                or decision.get("strategy") != version
                or decision["previous_sha256"] != signature(prev)
            ):
                raise ValueError("v21_frozen_decision_changed")
            notice = preopen_stop(project, book, folder, decision, at(target, "09:00"), stamp)
            summary.update(
                decision_status="stopped_preopen" if notice else decision["status"],
                decision_recorded_at=decision["recorded_at"],
                decision_cutoff=decision["cutoff"],
                last_decision=signal,
                candidate_count=len(decision["picks"]),
            )
            if entry_profile:
                summary.update(
                    candidate_count=len(decision.get("ranked", [])),
                    order_count=len(decision["picks"]),
                    no_buy_reason=decision.get("no_buy_reason"),
                    regime=decision.get("regime"),
                    target_budget=decision.get("target_budget"),
                )
            if now < at(target, "15:30"):
                return dict(**summary, status="waiting_close")
            try:
                columns, _, refs = dated_inputs(project / "data/jquants", [target], now)
                bars = {code: items[-1] for code, items in columns.items()}
            except (ValueError, FileNotFoundError):
                return dict(**summary, status="waiting_close_inputs")
            try:
                result = simulate(project, scratch, decision, capital, bars, prev, bool(notice))
            except ValueError as exc:
                return dict(**summary, status="needs_review", issue=str(exc))
            close = dict(
                target=target,
                previous_sha256=signature(prev),
                decision_sha256=signature(decision),
                preopen_stop_sha256=signature(notice) if notice else None,
                recorded_at=stamp().isoformat(),
                price_sources=refs,
                **result,
            )
            write(folder / "close.json", close)
            prev = close
