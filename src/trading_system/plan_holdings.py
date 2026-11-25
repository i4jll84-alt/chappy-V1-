"""Read-only paper holdings from account snapshots and verified saved daily prices."""

import json
import sqlite3
from contextlib import closing
from datetime import date, time
from decimal import Decimal, InvalidOperation
from time import monotonic
from zoneinfo import ZoneInfo

import duckdb

from trading_system.plan_performance import performance
from trading_system.research.daytrade_universe import dated_inputs

JST = ZoneInfo("Asia/Tokyo")
DISPLAY_ORDER = ("A", "A9", "A10", "B", "C")


def ordered_accounts(plans):
    return [k for k in DISPLAY_ORDER if k in plans] + [k for k in plans if k not in DISPLAY_ORDER]


def positive(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except InvalidOperation:
        return None


def saved_prices(project, now):
    root = project / "data/jquants"
    local = now.astimezone(JST)
    pending = False
    try:
        receipts = sorted(
            (root / "daily-snapshots").glob("????-??-??/last_collection.json"), reverse=True
        )
    except OSError:
        return {}, {}, None, "日足の保存先を確認できません。"
    for path in receipts:
        day = path.parent.name
        try:
            stamp = date.fromisoformat(day)
        except ValueError:
            continue
        if stamp > local.date() or (stamp == local.date() and local.time() < time(15, 30)):
            continue
        try:
            receipt = json.loads(path.read_bytes())
            if not isinstance(receipt, dict):
                raise ValueError("invalid daily receipt")
            if receipt.get("state") != "complete":
                pending = True
                continue
            columns, master, _ = dated_inputs(root, [day], now)
            return (
                {c: rows[-1] for c, rows in columns.items()},
                master,
                day,
                "新しい日足は保存未完了です。" if pending else "",
            )
        except (OSError, ValueError, KeyError, TypeError, IndexError, duckdb.Error):
            # Never substitute entry cost or silently use an older day after a verification error.
            return {}, {}, None, f"{day}の日足・銘柄情報を確認できません。"
    return {}, {}, None, "保存済み日足がありません。"


def positions(project, key, plan):
    if key == "C":
        return [
            dict(
                plan=key,
                code=code,
                shares=positive(pos["shares"]),
                entry_date=pos["entry_date"],
                entry_price=positive(pos["entry_price"]),
                acquisition_cost=positive(pos["cost"]),
                holding_date=plan.get("holdings_date") or plan.get("last_mark"),
                note="",
            )
            for code, pos in sorted(plan["positions"].items())
        ]
    if key == "A" or plan.get("adapter") == "plan_a_forward":
        held = plan.get("positions")
        if not isinstance(held, dict):
            raise ValueError("A holdings unavailable")
        result = []
        for code, p in sorted(held.items()):
            if not isinstance(code, str) or not isinstance(p, dict):
                raise ValueError("invalid A holdings")
            entry = positive(p.get("quote"))
            adjusted = positive(p.get("entry"))
            result.append(
                dict(
                    plan=key,
                    code=code,
                    shares=positive(p.get("shares")),
                    entry_date=p.get("entered"),
                    entry_price=entry,
                    acquisition_cost=positive(p.get("debit")),
                    holding_date=plan.get("last_mark"),
                    note="分割・併合あり（取得時価格）"
                    if entry and adjusted and entry != adjusted
                    else "",
                )
            )
        return result

    if key != "B":
        raise ValueError("unsupported account")
    expected = plan.get("positions")
    # A verified zero-position summary requires no native ledger reads.
    if expected == 0:
        return []
    capital, revision = int(plan["capital"]), int(plan["native_revision"])
    if capital <= 0 or revision < 1:
        raise ValueError("invalid native account")
    path = project / "data/a0-5-paper/accounts" / str(capital) / "ledger.sqlite"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        record = db.execute("SELECT body FROM revisions WHERE revision=?", (revision,)).fetchone()
    if record is None:
        raise ValueError("native revision missing")
    state = json.loads(record[0])
    held = state["positions"]
    if state["capital"] != capital or len(held) != expected or state["revision"] != revision:
        raise ValueError("native account mismatch")
    if not isinstance(held, list) or not all(isinstance(p, dict) for p in held):
        raise ValueError("invalid B holdings")
    result = []
    for p in sorted(held, key=lambda item: item["Code"]):
        if not isinstance(p["Code"], str):
            raise ValueError("invalid holding code")
        buys = [
            r
            for r in state["journal"]
            if r.get("Code") == p["Code"]
            and r.get("date") == p["entry_date"]
            and r.get("side") == "BUY"
            and r.get("status") == "仮想約定"
        ]
        buy = buys[0] if len(buys) == 1 else {}
        shares = positive(p.get("shares"))
        note = (
            "分割・併合あり（取得時価格）" if buy and shares != positive(buy.get("shares")) else ""
        )
        result.append(
            dict(
                plan=key,
                code=p["Code"],
                shares=shares,
                entry_date=p["entry_date"],
                entry_price=positive(buy.get("price")),
                acquisition_cost=positive(p.get("cost")),
                holding_date=max(plan.get("last_mark") or "", state.get("last_open") or "") or None,
                note=note,
            )
        )
    return result


def holdings_snapshot(project, plans, now):
    rows, statuses = [], []
    for key in ordered_accounts(dict.fromkeys(("A", "B", *plans))):
        if key not in plans:
            statuses.append(f"{key}：口座記録待ち")
            continue
        try:
            held = positions(project, key, plans[key])
            rows.extend(held)
            suffix = (
                f"（保有記録 {plans[key]['last_mark']}）" if plans[key].get("last_mark") else ""
            )
            statuses.append(f"{key}：保有{len(held)}銘柄" + suffix)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            statuses.append(f"{key}：保有記録を確認できません")
    prices, master, day, issue = saved_prices(project, now) if rows else ({}, {}, None, "")
    for row in rows:
        code = row["code"]
        row["name"] = master.get(code, {}).get("CoName") or "名称未確認"
        current = positive(prices.get(code, {}).get("C"))
        row.update(current_price=current, price_date=day)
        notes = [row["note"]] if row["note"] else []
        if row["entry_price"] is None:
            notes.append("取得価格未確認")
        if current is None:
            notes.append("終値欠損" if day else "終値未確認")
        net, percent = None, None
        if row["acquisition_cost"] is None:
            notes.append("取得総額未確認")
        elif row["shares"] is None:
            notes.append("株数未確認")
        elif day and row["holding_date"] != day:
            # Do not mix an unprocessed split (or an older price) with these shares.
            notes.append("損益未確認（保有評価日と終値日が不一致）")
        elif current is not None:
            net, percent = performance(current * row["shares"], row["acquisition_cost"])
        row.update(net=net, return_pct=percent)
        row["note"] = "／".join(notes)
    return dict(rows=rows, status="　　".join(statuses), issue=issue)


class HoldingsReader:
    """Refresh saved input checks at most every 30 seconds, or on account changes."""

    def __init__(self, project):
        self.project = project
        self.key = None
        self.loaded_at = None
        self.value = None

    def read(self, plans, now):
        key = (json.dumps(plans, sort_keys=True, ensure_ascii=False), now.astimezone(JST).date())
        tick = monotonic()
        if key != self.key or self.loaded_at is None or tick - self.loaded_at >= 30:
            value = holdings_snapshot(self.project, plans, now)
            self.key, self.loaded_at, self.value = key, tick, value
        return self.value
