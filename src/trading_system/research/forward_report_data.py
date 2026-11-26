"""Read-only, auditable forward-paper report data. No strategy or ledger writers."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from . import a0_5_book, a0_5_inputs, plan_a_forward, swing_forward
from .daytrade_universe import calendar_days, dated_inputs

JST = ZoneInfo("Asia/Tokyo")
ACTIVE_IDS = ("A", "B", "C", "A9", "A10")


class ReportInputError(ValueError):
    """An input cannot support a verified performance claim."""


def number(value):
    if value is None or isinstance(value, bool):
        raise ReportInputError("missing numeric input")
    value = float(value)
    if not math.isfinite(value):
        raise ReportInputError("nonfinite numeric input")
    return value


def same(actual, expected, message):
    if not math.isclose(number(actual), number(expected), abs_tol=0.02, rel_tol=1e-10):
        raise ReportInputError(message)


def logical_hash(value):
    return swing_forward.signature(value)


def period_metrics(curve, trades, *, start, end, plan_start, fees=()):
    """Daily realized-only return statistics, with the preceding available baseline.

    Curve starts at plan_start at initial capital. No MTM or purchase fees are
    debited a second time: each closed trade's net already contains both fees.
    """
    effective = max(start, plan_start)
    if effective > end:
        raise ReportInputError("period precedes account start")
    points = [r for r in curve if effective <= r["date"] <= end]
    preceding = [r for r in curve if r["date"] < effective]
    baseline = preceding[-1] if preceding else curve[0]
    if not points or points[-1]["date"] != end:
        raise ReportInputError("missing end-of-period close")
    changes = [r for r in points if r["date"] > baseline["date"]]
    equity = number(baseline["equity"])
    if equity <= 0:
        raise ReportInputError("nonpositive return baseline")
    peak, dd, daily = equity, 0.0, []
    for row in changes:
        new = number(row["equity"])
        daily.append(dict(date=row["date"], value=new / equity - 1))
        equity = new
        peak = max(peak, equity)
        dd = min(dd, equity / peak - 1)
    returns = [r["value"] for r in daily]
    sd = statistics.stdev(returns) if len(returns) >= 2 else None
    sharpe = statistics.mean(returns) / sd * math.sqrt(252) if sd else None
    closed = [r for r in trades if effective <= r["exit_date"] <= end]
    wins = [number(r["net"]) for r in closed if number(r["net"]) > 0]
    losses = [-number(r["net"]) for r in closed if number(r["net"]) < 0]
    ret = (equity / number(baseline["equity"]) - 1) * 100
    topix = (number(points[-1]["topix_close"]) / number(baseline["topix_close"]) - 1) * 100
    return dict(
        start=effective,
        effective_start=effective,
        end=end,
        baseline_date=baseline["date"],
        calendar_period_start=start,
        calendar_period_end=end,
        closed_profit_jpy=sum(number(r["net"]) for r in closed),
        realized_profit_jpy=sum(number(r["net"]) for r in closed),
        return_pct=ret,
        daily_returns=daily,
        daily_observations=len(daily),
        sharpe=sharpe,
        sharpe_reason=None if sharpe is not None else "fewer_than_two_returns_or_zero_variance",
        profit_factor=sum(wins) / sum(losses) if losses else None,
        profit_factor_reason=None
        if losses
        else "no_closed_losses"
        if wins
        else "no_closed_trades_or_gains",
        max_drawdown_pct=dd * 100,
        trade_count=len(closed),
        wins=len(wins),
        losses=len(losses),
        breakeven=len(closed) - len(wins) - len(losses),
        fees=sum(number(r["fee"]) for r in fees if effective <= r["date"] <= end),
        topix_return_pct=topix,
        excess_points=ret - topix,
        baseline_equity=number(baseline["equity"]),
        end_equity=equity,
    )


class _Reader:
    def __init__(self, project, now=None):
        self.now = now
        self.project = Path(project).resolve()
        self.manifest = {}

    def track(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.project):
            raise ReportInputError("source path leaves project")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        relative = path.relative_to(self.project).as_posix()
        if relative in self.manifest and self.manifest[relative] != digest:
            raise ReportInputError("input changed while report was reading: " + relative)
        self.manifest[relative] = digest
        return path

    def read(self, path):
        value = json.loads(self.track(path).read_bytes())
        if self.now and isinstance(value, dict) and value.get("recorded_at"):
            recorded = datetime.fromisoformat(value["recorded_at"])
            if recorded.tzinfo is None or recorded > self.now:
                raise ReportInputError("saved record was not available at report time")
        return value

    def references(self, refs, root):
        for ref in refs:
            if ref.get("manifest"):
                path = self.track(root / ref["manifest"])
                meta = json.loads(path.read_bytes())
                for page in meta.get("pages", []):
                    self.track(path.parent / page["file"])
            elif ref.get("page"):
                self.track(root / "reference/master" / ref["page"])
            elif ref.get("path"):
                self.track(root / "history" / ref["path"])


def _calendar(reader, start, asof, now):
    days = {}
    cursor = date.fromisoformat(start)
    anchors = []
    while cursor <= date.fromisoformat(asof):
        anchors.append(cursor)
        cursor += timedelta(days=25)
    if anchors[-1] != date.fromisoformat(asof):
        anchors.append(date.fromisoformat(asof))
    for anchor in anchors:
        found, refs = calendar_days(reader.project / "data/jquants", anchor, now)
        reader.references(refs, reader.project / "data/jquants")
        for day, flag in found.items():
            if day in days and days[day] != flag:
                raise ReportInputError("calendar disagreement")
            days[day] = flag
    last = date.fromisoformat(asof)
    needed = [
        str(date.fromisoformat(start) + timedelta(days=i))
        for i in range((last - date.fromisoformat(start)).days + 1)
    ]
    if not set(needed) <= days.keys() or days.get(asof) not in ("1", "2"):
        raise ReportInputError("missing calendar or asof is not a session")
    sessions = sorted(d for d, flag in days.items() if flag in ("1", "2"))
    future = [d for d in sessions if d > asof]
    if not future:
        raise ReportInputError("next trading session missing")
    return sessions, future[0]


def _topix(reader, days, now):
    result = {}
    root = reader.project / "data/jquants"
    folder = root / "market_financial/topix"
    for day in days:
        receipt = reader.read(root / "daily-snapshots" / day / "last_collection.json")
        refs = []
        rows = a0_5_inputs.market_pages(root, receipt, "topix", "Date", now, refs)
        if len(rows) != 1 or number(rows[0]["C"]) <= 0:
            raise ReportInputError(f"TOPIX missing or duplicate: {day}")
        result[day] = number(rows[0]["C"])
        with duckdb.connect(str(folder / "catalog.duckdb"), read_only=True) as db:
            pages = db.execute(
                "SELECT raw_path FROM pages WHERE job_id=?", [receipt["jobs"]["topix"]["job_id"]]
            ).fetchall()
        for (path,) in pages:
            reader.track(folder / path)
    return result


def _verify_decision(decision, target, *, allow_no_buy_late=False, now=None):
    recorded = datetime.fromisoformat(decision["recorded_at"])
    deadline = datetime.fromisoformat(target + "T09:00:00+09:00")
    signal = decision.get("signal_date", decision.get("date"))
    if recorded.tzinfo is None or (now is not None and recorded > now):
        raise ReportInputError("decision unavailable at report time")
    if not signal or signal >= target:
        raise ReportInputError("decision signal date invalid")
    status = decision.get("status", decision.get("state"))
    no_buy = (
        not decision.get("picks")
        and not decision.get("plan", {}).get("buys")
        and not any(r.get("side") == "BUY" for r in decision.get("orders", []))
    )
    if (
        allow_no_buy_late
        and no_buy
        and status in {"missed_preopen", "stopped", "stopped_preopen", "exits_only"}
    ):
        return
    cutoff = datetime.fromisoformat(decision["cutoff"])
    if cutoff.tzinfo is None or not cutoff <= recorded < deadline:
        raise ReportInputError("decision was not frozen before opening deadline")


def _stop(reader, path, close):
    stop_path = path.with_name("preopen_stop.json")
    value = reader.read(stop_path) if stop_path.exists() else None
    if close.get("preopen_stop_sha256") != (logical_hash(value) if value else None):
        raise ReportInputError("preopen stop changed")


def _a_account(reader, plan, asof, required):
    book = reader.project / "data/plan-forward/accounts" / plan["id"]
    state = reader.read(book / "initial.json")
    plan_a_forward.account_options(plan)
    strategy = plan_a_forward.STRATEGY_BY_TERM[plan_a_forward.FIXED_TERMS[plan["id"]][1]]
    if state != plan_a_forward.initial(plan["start"], plan["capital"], strategy=strategy):
        raise ReportInputError("A initial account changed")
    closed, fees, buys, marks = [], [], {}, []
    paths = [p for p in sorted((book / "sessions").glob("*/close.json")) if p.parent.name <= asof]
    if {p.parent.name for p in paths} != set(required):
        raise ReportInputError("missing or extra session close")
    for path in paths:
        close, decision = reader.read(path), reader.read(path.with_name("decision.json"))
        day = path.parent.name
        _verify_decision(
            decision,
            day,
            allow_no_buy_late=not any(e.get("action") == "BUY" for e in close["events"]),
            now=reader.now,
        )
        if (
            close["previous_sha256"] != logical_hash(state)
            or close["decision_sha256"] != logical_hash(decision)
            or close["target"] != day
            or decision["target"] != day
        ):
            raise ReportInputError("A close or frozen decision chain changed")
        _stop(reader, path, close)
        for event in close["events"]:
            if event.get("action") == "BUY":
                buys[(event["code"], event["date"])] = event
                fees.append(dict(date=event["date"], fee=number(event["cost"])))
        for trade in close["trades"]:
            if not trade.get("is_sale"):
                continue
            code, entry_day = trade["code"], trade["entry_day"]
            buy = buys.get((code, entry_day))
            if buy is None:
                raise ReportInputError("closed trade has no verified entry")
            net = number(trade["net"])
            same(
                net,
                number(trade["exit_proceeds"]) - number(trade["entry_debit"]),
                "A net cost mismatch",
            )
            fees.append(dict(date=day, fee=number(trade["exit_cost"])))
            closed.append(
                dict(
                    code=code[:4],
                    source_code=code,
                    entry_date=entry_day,
                    exit_date=trade["exit_day"],
                    shares=number(trade["shares_at_exit"]),
                    net=net,
                    entry_price=number(trade["entry_raw_open"]),
                    exit_price=number(trade["exit_price"]),
                    fees=number(buy["cost"]) + number(trade["exit_cost"]),
                )
            )
        state = close["account"]
        if state["last_mark"] != day:
            raise ReportInputError("A mark date mismatch")
        same(state["realized"], sum(t["net"] for t in closed), "A realized mismatch")
        marks.append(dict(date=day, realized=number(state["realized"])))
    state = deepcopy(state)
    state["unrealized"] = number(close["mark"]["unrealized"]) if paths else 0
    return state, closed, fees, marks


def _b_account(reader, plan, asof, required):
    book = reader.project / "data/plan-forward/accounts/B"
    base = reader.read(book / "baseline.json")
    native = reader.project / "data/a0-5-paper/accounts" / str(plan["capital"])
    db_path = reader.track(native / "ledger.sqlite")
    if (
        base["start"] != plan["start"]
        or base["initial_capital"] != plan["capital"]
        or a0_5_book.revision_digest(db_path, base["native_revision"])
        != base["native_revision_sha256"]
    ):
        raise ReportInputError("B baseline revision mismatch")
    # Select the last saved revision whose accounting/decision dates do not exceed asof.
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        records = db.execute("SELECT body FROM revisions ORDER BY revision DESC").fetchall()
        baseline_row = db.execute(
            "SELECT body FROM revisions WHERE revision=?", (base["native_revision"],)
        ).fetchone()
        baseline_state = json.loads(baseline_row[0])
        if len(baseline_state["journal"]) != base["journal_offset"]:
            raise ReportInputError("B baseline journal offset mismatch")
        state = None
        for (body,) in records:
            candidate = json.loads(body)
            if (candidate.get("last_open") or "") <= asof and max(
                candidate.get("plans", {}), default=""
            ) <= asof:
                state = candidate
                break
        if state is None:
            raise ReportInputError("B asof revision missing")
        for day, ref in list(state["plans"].items()):
            if isinstance(ref, str):
                row = db.execute("SELECT body FROM decisions WHERE digest=?", (ref,)).fetchone()
                if row is None or hashlib.sha256(row[0].encode()).hexdigest() != ref:
                    raise ReportInputError("B decision hash changed")
                state["plans"][day] = json.loads(row[0])
    if (
        state["capital"] != plan["capital"]
        or state["revision"] < base["native_revision"]
        or state["last_open"] != asof
    ):
        raise ReportInputError("B revision or final session missing")
    if len(state["journal"]) < base["journal_offset"]:
        raise ReportInputError("B journal truncated")
    decisions = state["plans"]
    for day, decision in decisions.items():
        if day >= plan["start"]:
            _verify_decision(
                decision, decision["order_date"], allow_no_buy_late=True, now=reader.now
            )
    if any(
        day not in decisions or decisions[day]["state"] != "ready"
        for day in [plan["start"], *required]
    ):
        raise ReportInputError("B daily mark missing or incomplete")
    if state["journal"][: base["journal_offset"]] != baseline_state["journal"]:
        raise ReportInputError("B baseline journal prefix changed")
    journal = state["journal"][base["journal_offset"] :]
    fills = [r for r in journal if r["status"] == "仮想約定"]
    closed, fees = [], []
    for row in fills:
        if not plan["start"] < row["date"] <= asof:
            raise ReportInputError("B comparison trade date invalid")
        fees.append(dict(date=row["date"], fee=number(row["fee"])))
        if row["realized_pnl"] is None:
            continue
        prior = [
            b
            for b in fills
            if b["Code"] == row["Code"] and b["side"] == "BUY" and b["date"] <= row["date"]
        ]
        if row["side"] != "SELL" or not prior:
            raise ReportInputError("B closed trade missing buy")
        buy = prior[-1]
        closed.append(
            dict(
                code=row["Code"][:4],
                source_code=row["Code"],
                entry_date=buy["date"],
                exit_date=row["date"],
                shares=number(row["shares"]),
                net=number(row["realized_pnl"]),
                entry_price=number(buy["price"]),
                exit_price=number(row["price"]),
                fees=number(buy["fee"]) + number(row["fee"]),
            )
        )
    positions = {}
    for pos in state["positions"]:
        buys = [
            b
            for b in fills
            if b["side"] == "BUY" and b["Code"] == pos["Code"] and b["date"] == pos["entry_date"]
        ]
        if len(buys) != 1:
            raise ReportInputError("B position buy missing")
        positions[pos["Code"]] = dict(
            shares=pos["shares"],
            entry_date=pos["entry_date"],
            entry_price=buys[0]["price"],
            cost=pos["cost"],
        )
    realized = sum(t["net"] for t in closed)
    mark = decisions[asof]
    account = dict(
        cash=state["cash"],
        nav=mark["equity"],
        realized=realized,
        positions=positions,
        last_mark=asof,
        native_revision=state["revision"],
        next_decision=mark,
    )
    account["unrealized"] = number(account["nav"]) - plan["capital"] - realized
    return account, closed, fees, []


def _c_account(reader, plan, asof, required):
    from .pead_source import PEAD_SOURCE_COMMIT
    from .plan_c_pead_forward import VERSION, account

    book = reader.project / "data/plan-forward/accounts/C"
    initial = reader.read(book / "initial.json")
    if (
        initial["version"] != VERSION
        or initial["version"] != plan["version"]
        or initial["start"] != plan["start"]
        or initial["initial_capital"] != plan["capital"]
        or initial.get("source_commit") != PEAD_SOURCE_COMMIT
        or initial.get("strategy") != "GRID_DIP_H60_ATR40"
    ):
        raise ReportInputError("active C configuration mismatch")
    # Exact pure initial operator state, from the pinned daily_ops.status schema.
    native = dict(
        schema_version=1,
        account="plan_c_pead_v1",
        strategy="GRID_DIP_H60_ATR40",
        last_processed_date="",
        settled_cash=float(plan["capital"]),
        positions=[],
        unsettled=[],
        pending_buys=[],
        pending_target=None,
        alerts=[],
        plans={},
        receipts={},
        fills={},
        daily_journal=None,
        last_snapshot=None,
    )
    state, closed, fees, lots, seen = account(native, plan["capital"]), [], [], {}, set()
    paths = [p for p in sorted((book / "sessions").glob("*/close.json")) if p.parent.name <= asof]
    if {p.parent.name for p in paths} != set(required):
        raise ReportInputError("C session close missing")
    for path in paths:
        close, decision = reader.read(path), reader.read(path.with_name("decision.json"))
        day = path.parent.name
        _verify_decision(
            decision,
            day,
            allow_no_buy_late=not any(
                f.get("side") == "BUY" and f.get("date") == day
                for fills in close["operator_state"]["fills"].values()
                for f in fills
            ),
            now=reader.now,
        )
        if (
            close["version"] != plan["version"]
            or close["date"] != day
            or close["decision_sha256"] != logical_hash(decision)
            or close["previous_native_sha256"] != logical_hash(native)
            or decision["starting_account_sha256"] != logical_hash(native)
            or close["native_sha256"] != logical_hash(close["operator_state"])
            or close["bars_sha256"] != logical_hash(close["snapshot"])
        ):
            raise ReportInputError("C frozen chain changed")
        _stop(reader, path, close)
        expected = account(
            close["operator_state"],
            plan["capital"],
            day,
            state["peak_nav"],
            state["max_drawdown_pct"],
        )
        if expected != close["account"]:
            raise ReportInputError("C account snapshot mismatch")
        for key, fills in close["operator_state"]["fills"].items():
            for index, fill in enumerate(fills):
                ident = (key, index, logical_hash(fill))
                if ident in seen:
                    continue
                seen.add(ident)
                code, qty = fill["code"], number(fill["shares"])
                fees.append(dict(date=fill["date"], fee=number(fill["fee"])))
                if fill["side"] == "BUY":
                    if code in lots:
                        raise ReportInputError("C unmatched multiple entry lots")
                    lots[code] = dict(
                        fill, remaining=qty, unit_cost=number(fill["net_amount"]) / qty
                    )
                elif fill["side"] == "SELL":
                    buy = lots.get(code)
                    if not buy or qty > buy["remaining"]:
                        raise ReportInputError(
                            "C sale cost basis unverified (possible corporate action)"
                        )
                    basis = buy["unit_cost"] * qty
                    closed.append(
                        dict(
                            code=code[:4],
                            source_code=code,
                            entry_date=buy["date"],
                            exit_date=fill["date"],
                            shares=qty,
                            net=number(fill["net_amount"]) - basis,
                            entry_price=number(buy["price"]),
                            exit_price=number(fill["price"]),
                            fees=number(buy["fee"]) * qty / number(buy["shares"])
                            + number(fill["fee"]),
                        )
                    )
                    buy["remaining"] -= qty
                    if not buy["remaining"]:
                        del lots[code]
                else:
                    raise ReportInputError("unknown C fill side")
        native, state = close["operator_state"], deepcopy(close["account"])
        if not state.get("valuation_complete", True) or state.get("entitlement_review"):
            raise ReportInputError("C corporate-action entitlement valuation unresolved")
        same(
            state["realized"],
            sum(r["net"] for r in closed),
            "C realized and matched fill cost mismatch",
        )
    return state, closed, fees, []


def _holdings(state, plan, prices, master, asof):
    result = []
    for code, pos in sorted(state["positions"].items()):
        if plan["adapter"] == "plan_a_forward":
            entry_date, entry_price, cost = pos["entered"], pos["quote"], pos["debit"]
        else:
            entry_date, entry_price, cost = pos["entry_date"], pos["entry_price"], pos["cost"]
        row = prices.get(code)
        if (
            row is None
            or row.get("Date") != asof
            or row.get("C") is None
            or state["last_mark"] != asof
        ):
            raise ReportInputError(f"holding close missing or stale: {code}")
        shares, current, cost = number(pos["shares"]), number(row["C"]), number(cost)
        value = shares * current
        result.append(
            dict(
                code=code[:4],
                source_code=code,
                name=master.get(code, {}).get("CoName") or pos.get("name") or "名称未確認",
                shares=shares,
                entry_date=entry_date,
                entry_price=number(entry_price),
                current_price=current,
                price_date=asof,
                cost=cost,
                market_value=value,
                unrealized_jpy=value - cost,
                unrealized_pct=(value / cost - 1) * 100,
                note="取得費用込み。将来の売却費用は未控除",
            )
        )
    total = sum(r["unrealized_jpy"] for r in result)
    same(total, state["unrealized"], "holdings unrealized differs from account snapshot")
    same(
        number(state["cash"]) + sum(r["market_value"] for r in result),
        state["nav"],
        "holdings market value differs from account NAV",
    )
    return result


def _next_stop(reader, plan, decision, target):
    project = reader.project
    book = project / "data/plan-forward/accounts" / plan["id"]
    switches = [book / "STOP"]
    if plan["id"] == "B":
        switches += [project / "data/a0-5-paper/STOP", project / "data/swing-forward/STOP"]
    elif plan["id"] == "C":
        switches += [project / "data/plan-forward/STOP"]
    else:
        switches += [project / "data/swing-forward/STOP"]
    active = [reader.track(p).relative_to(project).as_posix() for p in switches if p.exists()]
    if plan["id"] == "B":
        native = project / "data/a0-5-paper"
        for stop_path in (
            native / "accounts" / str(plan["capital"]) / "stops" / (target + ".json"),
            native / "stops" / (target + ".json"),
        ):
            if stop_path.is_file():
                notice = reader.read(stop_path)
                observed = datetime.fromisoformat(notice["observed_at"])
                if observed.tzinfo is None or (reader.now is not None and observed > reader.now):
                    raise ReportInputError("native B stop unavailable at report time")
                return "保存済みのB口座停止により新規買付を停止"
        preopen = project / "data/swing-forward/sessions" / target / "preopen.json"
        if preopen.is_file():
            notice = reader.read(preopen)
            if notice.get("target", target) != target:
                raise ReportInputError("B common preopen stop target mismatch")
            if notice.get("stopped") is True:
                return "保存済みの共通寄付前停止により新規買付を停止"
    path = book / "sessions" / target / "preopen_stop.json"
    if path.exists():
        notice = reader.read(path)
        observed = datetime.fromisoformat(notice.get("observed_at", notice.get("recorded_at")))
        recorded = datetime.fromisoformat(decision["recorded_at"])
        deadline = datetime.fromisoformat(target + "T09:00:00+09:00")
        if (
            notice.get("target", target) != target
            or notice["decision_sha256"] != logical_hash(decision)
            or not recorded <= observed < deadline
            or (reader.now is not None and observed > reader.now)
        ):
            raise ReportInputError("next preopen stop evidence invalid")
        return "保存済みの寄付前停止により買付候補は取消済み"
    if active:
        return "停止スイッチを確認したため新規買付を停止: " + ", ".join(active)
    return None


def _planned(reader, plan, state, next_day, master):
    if plan["id"] == "B":
        decision = state["next_decision"]
        if decision.get("order_date") != next_day or decision.get("state") != "ready":
            raise ReportInputError("B next frozen plan missing")
        picks = [r for r in decision["orders"] if r["side"] == "BUY"]
        reason = decision.get("reason") or (
            "地合い等の固定条件により買付予定なし" if not picks else None
        )
    else:
        path = (
            reader.project
            / "data/plan-forward/accounts"
            / plan["id"]
            / "sessions"
            / next_day
            / "decision.json"
        )
        decision = reader.read(path)
        if decision["target"] != next_day or decision.get("status", decision.get("state")) not in {
            "ready",
            "stopped",
            "stopped_preopen",
            "missed_preopen",
            "exits_only",
        }:
            raise ReportInputError("next frozen decision missing or incomplete")
        if plan["id"] == "C" and decision["starting_account_sha256"] != state.get("_native_hash"):
            raise ReportInputError("C next decision chain changed")
        picks = decision.get("picks", [])
        reason = (
            decision.get("no_buy_reason")
            or decision.get("reason")
            or (
                decision.get("status", decision.get("state"))
                if decision.get("status", decision.get("state")) != "ready"
                else None
            )
        )
    _verify_decision(decision, next_day, allow_no_buy_late=True, now=reader.now)
    stopped_reason = _next_stop(reader, plan, decision, next_day)
    if stopped_reason:
        reason = stopped_reason
    held = set(state["positions"])
    rows = []
    for pick in picks:
        code = pick.get("code", pick.get("Code"))
        already = code in held
        rows.append(
            dict(
                code=code[:4],
                source_code=code,
                name=pick.get("name") or master.get(code, {}).get("CoName") or "名称未確認",
                shares=number(pick.get("shares", pick.get("planned_shares", 100))),
                prev_close=number(pick["prev_close"])
                if pick.get("prev_close") is not None
                else None,
                rank=pick.get("rank"),
                target=next_day,
                status="canceled_stopped"
                if stopped_reason
                else "skip_already_held"
                if already
                else "conditional",
                reason=stopped_reason
                if stopped_reason
                else "既保有のため新規買付対象外"
                if already
                else "寄付価格・ギャップ条件・買付余力等の確認後に模擬約定。確定注文ではありません",
            )
        )
    return rows, reason, decision


def period_coverage(*, asof, next_session, period_start, period_end, plan_start):
    """An exchange period closes on its last session, including holiday endings."""
    closed = next_session > period_end
    return dict(
        calendar_period_end=period_end,
        period_closed=closed,
        partial=(not closed or plan_start > period_start),
        short_history=(plan_start > period_start),
        asof=asof,
    )


def build_report_packet(project, asof, *, now=None):
    """Return a detached JSON-compatible snapshot without network or ledger writes.

    Integrity failures are explicit unavailable plan records; shared market-input
    failures raise ReportInputError. No missing return is converted to zero.
    """
    asof = date.fromisoformat(str(asof)).isoformat()
    now = now or datetime.now(JST)
    if now.tzinfo is None or now.astimezone(JST).date().isoformat() < asof:
        raise ReportInputError("invalid report availability timestamp")
    reader = _Reader(project, now)
    registry = reader.read(reader.project / "config/forward-plans.json")["plans"]
    active = {p["id"]: p for p in registry if p["stage"] == "active"}
    if set(active) != set(ACTIVE_IDS):
        raise ReportInputError("active plan registry changed; review report mapping")
    start = min(p["start"] for p in active.values())
    sessions, next_day = _calendar(reader, start, asof, now)
    days = [d for d in sessions if start <= d <= asof]
    topix = _topix(reader, days, now)
    cols, master, refs = dated_inputs(reader.project / "data/jquants", [asof], now)
    reader.references(refs, reader.project / "data/jquants")
    prices = {c: rows[-1] for c, rows in cols.items()}
    packet = dict(
        schema_version=1,
        asof=asof,
        generated_at=now.isoformat(),
        next_session=next_day,
        plans={},
        methodology=dict(
            equity="initial_capital_plus_cumulative_closed_trade_net_pnl",
            unrealized="holdings_only_excluded_from_performance_curve",
            fees=(
                "closed net includes entry and exit costs once; "
                "period fees include all executed entry/exit fees"
            ),
            sharpe=(
                "daily realized returns; saved exchange sessions including zero-return days; "
                "rf=0; sample SD; sqrt(252)"
            ),
            benchmark=(
                "TOPIX price index normalized at the identical plan/period baseline; no gap filling"
            ),
            paper_only=True,
        ),
    )
    for key in ACTIVE_IDS:
        plan = active[key]
        row = dict(registry=deepcopy(plan), **deepcopy(plan), status="unavailable", issues=[])
        packet["plans"][key] = row
        try:
            if plan["start"] not in days:
                raise ReportInputError("account baseline close missing")
            required = [d for d in days if d > plan["start"]]
            loader = _b_account if key == "B" else _c_account if key == "C" else _a_account
            state, closed, fees, _ = loader(reader, plan, asof, required)
            if key == "C":
                close = reader.read(
                    reader.project / "data/plan-forward/accounts/C/sessions" / asof / "close.json"
                )
                state["_native_hash"] = close["native_sha256"]
            holdings = _holdings(state, plan, prices, master, asof)
            for trade in closed:
                trade["name"] = master.get(trade["source_code"], {}).get("CoName") or "名称未確認"
            cumulative, curve = 0.0, []
            byday = defaultdict(float)
            for trade in closed:
                byday[trade["exit_date"]] += trade["net"]
            for day in [plan["start"], *required]:
                cumulative += byday[day]
                curve.append(
                    dict(
                        date=day,
                        equity=plan["capital"] + cumulative,
                        realized_pnl=byday[day],
                        topix_close=topix[day],
                        topix_equity=plan["capital"] * topix[day] / topix[plan["start"]],
                    )
                )
            same(cumulative, state["realized"], "closed PnL and account realized mismatch")
            next_status = "complete"
            try:
                planned, no_buy_reason, decision = _planned(reader, plan, state, next_day, master)
            except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
                next_status = "unavailable"
                planned, no_buy_reason, decision = [], "次営業日の買付判断は未確認／未確定", {}
                row["issues"].append(f"next_plan: {type(error).__name__}: {error}")
            periods = {}
            asof_date = date.fromisoformat(asof)
            begins = dict(
                day=asof,
                week=str(asof_date - timedelta(days=asof_date.weekday())),
                year=f"{asof_date.year}-01-01",
                inception=plan["start"],
            )
            for period, begins_at in begins.items():
                metric = period_metrics(
                    curve, closed, start=begins_at, end=asof, plan_start=plan["start"], fees=fees
                )
                end_calendar = (
                    str(asof_date + timedelta(days=6 - asof_date.weekday()))
                    if period == "week"
                    else f"{asof_date.year}-12-31"
                    if period == "year"
                    else asof
                )
                metric.update(
                    period_coverage(
                        asof=asof,
                        next_session=next_day,
                        period_start=begins_at,
                        period_end=end_calendar,
                        plan_start=plan["start"],
                    )
                )
                periods[period] = metric
            row.update(
                status="complete" if next_status == "complete" else "partial",
                history_status="complete",
                next_plan_status=next_status,
                operational_status="ready"
                if next_status == "complete"
                else "waiting_next_decision",
                holdings=holdings,
                unrealized_jpy=sum(r["unrealized_jpy"] for r in holdings),
                closed_trades=closed,
                cash=number(state["cash"]),
                nav=number(state["nav"]),
                realized_jpy=cumulative,
                fees=sum(r["fee"] for r in fees),
                planned_buys=planned,
                no_buy_reason=no_buy_reason,
                next_decision=dict(
                    recorded_at=decision.get("recorded_at"),
                    origin=decision.get("decision_origin"),
                    target=next_day,
                ),
                realized_curve=curve,
                periods=periods,
            )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            sqlite3.Error,
            duckdb.Error,
        ) as error:
            row["issues"].append(f"{type(error).__name__}: {error}")
    packet["status"] = (
        "complete"
        if all(p["status"] == "complete" for p in packet["plans"].values())
        else "partial"
    )
    # Refuse a mixed snapshot if a concurrent collector or account writer changed
    # any consumed input while the report packet was assembled.
    for relative in list(reader.manifest):
        reader.track(reader.project / relative)
    packet["input_manifest"] = [
        dict(path=path, sha256=digest) for path, digest in sorted(reader.manifest.items())
    ]
    packet["dataset_sha256"] = logical_hash(
        {k: v for k, v in packet.items() if k not in ("generated_at", "dataset_sha256")}
    )
    return packet
