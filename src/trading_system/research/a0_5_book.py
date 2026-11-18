"""Atomic, independent A0_5 paper ledger. All executions are simulated cash-equity fills."""

import hashlib
import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from copy import deepcopy

import pandas as pd

from .a0_5_rules.config import A0_5
from .a0_5_rules.data_fetcher import iso
from .a0_5_rules.live_portfolio import buying_power


def initial(capital=A0_5.capital):
    return dict(
        revision=0,
        capital=int(capital),
        cash=float(capital),
        positions=[],
        settlements=[],
        journal=[],
        plans={},
        openings={},
        manual={},
        started_at=None,
        last_open=None,
    )


def revision_digest(db_path, revision):
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT body FROM revisions WHERE revision=?", (revision,)).fetchone()
    if row is None:
        raise ValueError("Bプラン開始時のA0-5台帳の版を確認できません")
    return hashlib.sha256(row[0].encode("utf-8")).hexdigest()


def load(root, capital=A0_5.capital):
    path = root / "ledger.sqlite"
    if not path.is_file():
        return initial(capital)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT body FROM revisions ORDER BY revision DESC LIMIT 1").fetchone()
        state = json.loads(row[0]) if row else initial(capital)
        if int(state.get("capital", capital)) != int(capital):
            raise ValueError("ペーパー口座の元本と台帳が一致しません")
        for day, ref in state["plans"].items():
            if isinstance(ref, str):
                payload = db.execute("SELECT body FROM decisions WHERE digest=?", (ref,)).fetchone()
                if not payload or hashlib.sha256(payload[0].encode()).hexdigest() != ref:
                    raise ValueError(f"{day} 予定の版を検証できません")
                state["plans"][day] = json.loads(payload[0])
        return state


def save(root, state):
    # Writers also hold the project-scoped OS lock for the whole read/plan/commit sequence.
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "ledger.sqlite")) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS revisions(revision INTEGER PRIMARY KEY,body TEXT NOT NULL)"
        )
        revision = db.execute("SELECT coalesce(max(revision),0) FROM revisions").fetchone()[0]
        if revision != state["revision"]:
            raise ValueError("口座版の競合")
        # Large immutable input manifests are shared by all balance revisions.
        db.execute(
            "CREATE TABLE IF NOT EXISTS decisions(digest TEXT PRIMARY KEY,body TEXT NOT NULL)"
        )
        decision_versions = {}
        for day, plan in state["plans"].items():
            body = json.dumps(plan, ensure_ascii=False, allow_nan=False, sort_keys=True)
            digest = hashlib.sha256(body.encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?)", (digest, body))
            decision_versions[day] = digest
        new = dict(state, revision=revision + 1, plans=decision_versions)
        db.execute(
            "INSERT INTO revisions VALUES(?,?)",
            (new["revision"], json.dumps(new, ensure_ascii=False, allow_nan=False)),
        )
        state.update(revision=new["revision"])


def execute(state, day, orders, prices, calendar, refs):
    """Book one verified session as a unit, even after a crash or retry."""
    if day in state["openings"]:
        return state
    s = deepcopy(state)
    if s["last_open"] and s["last_open"] >= day:
        raise ValueError("寄付の記帳順序を確認してください")
    i = calendar.get_loc(pd.Timestamp(day))
    if i + 2 >= len(calendar):
        raise ValueError("T+2カレンダー不足")
    due = iso(calendar[i + 2])
    held = {p["Code"]: p for p in s["positions"]}
    for code, p in held.items():
        row = prices.get(code)
        if row is None or row.get("AdjFactor") in (None, ""):
            raise ValueError(f"{day} {code} 保有中の権利情報が未確認")
        factor = float(row["AdjFactor"])
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError(f"{code} 調整係数異常")
        if factor != 1:
            qty = p["shares"] / factor
            if (
                str(row.get("ExRT")) not in ("1", "2")
                or abs(qty - round(qty)) > 0.01
                or round(qty) % A0_5.lot
            ):
                raise ValueError(f"{code} 権利処理の株数を要確認")
            p["shares"] = round(qty)
            p["warning"] = "分割・併合後の株数と10日線を確認"
            # Total cost basis includes the original entry fee and is unchanged.
    seen = set()
    s["settlements"] = [r for r in s["settlements"] if r["date"] > day]
    for order in sorted(orders, key=lambda o: o["side"] == "BUY"):
        code, side = order["Code"], order["side"]
        key = f"A0_5/paper/{day}/{side}/{code}"
        if key in seen or order["order_date"] != day or side not in ("BUY", "SELL"):
            raise ValueError("予定の重複・日付不一致")
        seen.add(key)
        r = prices.get(code, {})
        opening = float(r["O"]) if r.get("O") not in (None, "") else None
        p = held.get(code)
        qty = p["shares"] if p and side == "SELL" else order["shares"]
        if side == "SELL" and not p:
            raise ValueError("売却予定と保有が不一致")
        reason = ""
        if opening is not None and not math.isfinite(opening):
            opening = None
        if order.get("stop_observed"):
            reason = "ペーパー停止を観測・仮想約定なし"
        elif opening is None or not math.isfinite(opening) or opening <= 0:
            reason = "始値欠損"
        elif side == "BUY" and (
            r.get("AdjFactor") in (None, "")
            or float(r["AdjFactor"]) != 1
            or r.get("ExRT") not in ("", None, "0")
        ):
            reason = "権利落ち・権利情報未確定"
        elif side == "BUY" and (
            code in held
            or len(held) >= 5
            or Counter(p["S33"] for p in held.values())[order["S33"]] >= 2
        ):
            reason = "売り未成立・保有枠不足"
        prior_cash = buying_power(s, day)
        prior_count = len(held)
        prior_sector = sum(pos["S33"] == order["S33"] for pos in held.values())
        price = fee = pnl = None
        if not reason:
            price = opening * (1 + (1 if side == "BUY" else -1) * A0_5.slip_bps / 10000)
            value = price * qty
            fee = value * A0_5.fee_bps / 10000
            if side == "BUY" and value + fee > buying_power(s, day):
                reason = "受渡拘束控除後の寄付資金不足"
            elif side == "BUY":
                held[code] = dict(
                    Code=code, S33=order["S33"], entry_date=day, shares=qty, cost=value + fee
                )
                s["cash"] -= value + fee
            else:
                pnl = value - fee - held.pop(code)["cost"]
                s["cash"] += value - fee
                s["settlements"].append(dict(date=due, amount=value - fee))
        if reason and side == "SELL":
            p["pending_exit"] = order["reason"]
        s["journal"].append(
            dict(
                id=key,
                Code=code,
                side=side,
                date=day,
                reason=order["reason"],
                status=reason or "仮想約定",
                shares=0 if reason else qty,
                opening=opening,
                price=None if reason else price,
                fee=0 if reason else fee,
                realized_pnl=pnl,
                settlement=due if side == "SELL" and not reason else None,
                planned_shares=order["shares"],
                cash_before=prior_cash,
                holdings_before=prior_count,
                sector_before=prior_sector,
                model_slip_bp=None if reason else A0_5.slip_bps,
                actual_slip_bp=None,
            )
        )
    s.update(positions=list(held.values()), last_open=day)
    s["openings"][day] = refs
    return s


def audit(s):
    filled = [r for r in s["journal"] if r["status"] == "仮想約定"]
    closed = [r for r in filled if r["realized_pnl"] is not None]
    pnl = [r["realized_pnl"] for r in closed]
    wins, losses = [p for p in pnl if p > 0], [-p for p in pnl if p < 0]
    peak = equity = s["capital"]
    dd = 0
    daily = {}
    for r in closed:
        daily[r["date"]] = daily.get(r["date"], 0) + r["realized_pnl"]
    for value in daily.values():
        equity += value
        peak = max(peak, equity)
        dd = min(dd, equity / peak - 1)

    def follows_plan(r):
        expected_price = r["opening"] * (1 + (0.001 if r["side"] == "BUY" else -0.001))
        value = r["price"] * r["shares"]
        valid = (
            r["shares"] > 0
            and r["shares"] % 100 == 0
            and math.isclose(r["price"], expected_price)
            and math.isclose(r["fee"], value * 0.0005)
        )
        if r["side"] == "BUY":
            valid = (
                valid
                and r["shares"] == r["planned_shares"]
                and value + r["fee"] <= r["cash_before"]
                and r["holdings_before"] < 5
                and r["sector_before"] < 2
            )
        return valid

    # These are modeled fills only. Actual execution cost differences require a real fill.
    return dict(
        fills=len(filled),
        closed=len(pnl),
        realized=sum(pnl),
        win_rate=len(wins) / len(pnl) if pnl else None,
        pf=sum(wins) / sum(losses) if losses else None,
        payoff=(sum(wins) / len(wins)) / (sum(losses) / len(losses)) if wins and losses else None,
        realized_mdd=dd if pnl else None,
        compliance=sum(follows_plan(r) for r in filled) / len(filled) if filled else None,
        model_buy_bp=A0_5.slip_bps,
        model_sell_bp=A0_5.slip_bps,
        model_one_way_bp=A0_5.slip_bps,
        actual_buy_bp=None,
        actual_sell_bp=None,
        actual_one_way_bp=None,
        actual_cost_difference=None,
        alert_bp=25,
    )
