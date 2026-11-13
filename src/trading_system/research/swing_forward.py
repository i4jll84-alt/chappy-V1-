"""Prospective swing decisions, independent paper accounts and end-of-day reconciliation."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from trading_system.brokers.locking import single_writer
from trading_system.research.daytrade_universe import JST, calendar_days, dated_inputs
from trading_system.research.swing_credit import capture, read_credit
from trading_system.research.swing_rule_review import save_review
from trading_system.research.swing_windows import history_rows
from trading_system.storage.store import atomic_write, canonical

START = "2026-11-16"
CAPITAL = Decimal(2000000)
ACCOUNTS = ("machine_current", "machine_plus3", "agent_current", "agent_plus3")
SLIP = Decimal("0.0005")
FEE = Decimal(10)
LOT = 100


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    atomic_write(path, canonical(value).encode(), sharing_retries=3)


def signature(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def initial():
    return {
        key: {
            "cash": str(CAPITAL),
            "positions": {},
            "realized": "0",
            "fees": "0",
            "slippage": "0",
            "high": str(CAPITAL),
            "max_dd": "0",
            "status": "ok",
        }
        for key in ACCOUNTS
    }


def money(raw):
    return Decimal(str(raw)) if raw not in (None, "") else None


def cost(close):
    return close * LOT * (1 + SLIP) + FEE


def calendar(jq, day, asof):
    combined = {}
    for pivot in (day - timedelta(days=25), day):
        entries, _ = calendar_days(jq, pivot, asof)
        combined.update(entries)
    sessions = sorted(d for d, h in combined.items() if h in ("1", "2"))
    return sessions


def ranks(history, master, window, balances):
    pool = []
    counts = Counter()
    for code, info in master.items():
        if (
            info["Mkt"] not in ("0111", "0112", "0113")
            or info["ProdCat"] != "011"
            or not code.endswith("0")
        ):
            counts["universe_excluded"] += 1
            continue
        rows = [history[d].get(code) for d in window]
        if any(not r or not money(r["C"]) or money(r["C"]) <= 0 for r in rows):
            counts["incomplete_close"] += 1
            continue
        if any(money(r["AdjFactor"]) != 1 or r["ExRT"] not in (None, "") for r in rows[1:]):
            counts["price_adjustment_or_rights"] += 1
            continue
        closes = [money(r["C"]) for r in rows]
        c = closes[-1]
        va = money(rows[-1]["Va"])
        if va is None or va < 100000000:
            counts["low_turnover"] += 1
            continue
        if cost(c) > 200000:
            counts["lot_over_budget"] += 1
            continue
        if c <= closes[0]:
            counts["nonpositive_momentum"] += 1
            continue
        ma5 = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20
        prior = [money(r["Va"]) for r in rows[-6:-1]]
        avg = sum(prior) / 5 if all(v is not None and v > 0 for v in prior) else None
        issues = []
        if not c > ma5 > ma20:
            issues.append("ma")
        if avg is None or va <= avg:
            issues.append("turnover_growth")
        diff = None
        restriction = None
        if balances is None:
            issues.append("credit_snapshot_unavailable")
        else:
            old, new = [b.get(code, {}) for b in balances]
            restriction = new.get("制限措置等")
            values = [
                r.get(k, "").replace(",", "").strip()
                for r in (old, new)
                for k in ("融資残高（株）", "貸株残高（株）")
            ]
            if not all(v.isascii() and v.isdigit() for v in values) or max(map(int, values)) == 0:
                issues.append("credit_missing")
            else:
                f0, l0, f1, l1 = map(int, values)
                diff = (f1 - l1) - (f0 - l0)
                if diff > 0:
                    issues.append("net_financing_increased")
                if restriction != "":
                    issues.append("credit_restriction")
        pool.append(
            dict(
                code=code,
                name=info["CoName"],
                close=str(c),
                return20=str(c / closes[0] - 1),
                turnover=str(va),
                sma5=str(ma5),
                sma20=str(ma20),
                prior_turnover_mean=str(avg) if avg else None,
                net_change=diff,
                restriction=restriction,
                plus3_exclusions=issues,
                estimated_debit=str(cost(c)),
            )
        )
    pool.sort(key=lambda r: (-Decimal(r["return20"]), r["code"]))
    return pool, dict(counts)


def screen(project, day, asof):
    jq = project / "data/jquants"
    sessions = calendar(jq, day, asof)
    i = sessions.index(str(day))
    window = sessions[i - 20 : i + 1]
    if len(window) != 21:
        raise ValueError("incomplete_calendar")
    historical = [
        d for d in window if not (jq / "daily-snapshots" / d / "last_collection.json").exists()
    ]
    history = {}
    archive = jq / "history"
    archive_refs = []
    if historical:
        wal = archive / "history.sqlite-wal"
        if wal.exists() and wal.stat().st_size:
            raise ValueError("history_busy")
        with sqlite3.connect(
            (archive / "history.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True
        ) as db:
            for r in history_rows(
                db,
                archive,
                "/equities/bars/daily",
                ["Date", "Code", "C", "Va", "ExRT", "AdjFactor"],
                min(historical),
                max(historical),
                archive_refs,
            ):
                if r["Date"] in historical:
                    part = history.setdefault(r["Date"], {})
                    if r["Code"] in part:
                        raise ValueError("duplicate_price")
                    part[r["Code"]] = r
        if any(datetime.fromisoformat(r["available_at"]) > asof for r in archive_refs):
            raise ValueError("late_archive")
    daily = [d for d in window if d not in historical]
    if not daily or daily[-1] != str(day):
        raise ValueError("decision_day_snapshot_missing")
    columns, master, refs = dated_inputs(jq, daily, asof)
    for code, rows in columns.items():
        for r in rows:
            history.setdefault(r["Date"], {})[code] = r
    if set(history) != set(window):
        raise ValueError("missing_window_days")
    credit_refs = []
    credit = []
    for d in window[-2:]:
        try:
            obs, meta = read_credit(project, d, asof)
            credit.append(obs)
            credit_refs.append(meta)
        except (FileNotFoundError, ValueError) as exc:
            credit_refs.append(
                dict(
                    date=d,
                    issue=type(exc).__name__ if isinstance(exc, FileNotFoundError) else str(exc),
                )
            )
    rows, excluded = ranks(history, master, window, credit if len(credit) == 2 else None)
    return dict(
        asof=asof.isoformat(),
        date=str(day),
        window=window,
        input_refs=refs + archive_refs,
        credit_refs=credit_refs,
        credit_complete=len(credit) == 2,
        universe_exclusions=excluded,
        candidates=rows,
    )


def account_orders(account, eligible, day, available=True):
    blocked = not available or account["status"] != "ok"
    sells = [c for c, p in account["positions"].items() if p["exit"] <= day] if not blocked else []
    cash = Decimal(account["cash"])
    buy = []
    skipped = {}
    for row in eligible[:5]:
        c = row["code"]
        debit = Decimal(row["estimated_debit"])
        if blocked:
            skipped[c] = "ledger_or_decision_unavailable"
        elif c in account["positions"]:
            skipped[c] = "already_held"
        elif cash < debit:
            skipped[c] = "cash_short_estimated"
        else:
            buy.append(c)
            cash -= debit
    return dict(buy=buy, sell=sells, skips=skipped)


def get_books(root, prior):
    if prior < START:
        return read(root / "initial.json")["accounts"]
    return read(root / "sessions" / prior / "close.json")["accounts"]


def morning(project, root, day, sessions, now):
    folder = root / "sessions" / day
    target = folder / "preopen.json"
    if target.exists():
        return "already_frozen"
    i = sessions.index(day)
    prev = sessions[i - 1]
    if now.time() >= time(9):
        return "missed_preopen"
    accounts = get_books(root, prev)
    cutoff = datetime.combine(date.fromisoformat(prev), time(23, 59, 59), JST)
    try:
        signal = screen(project, date.fromisoformat(prev), cutoff)
    except (FileNotFoundError, ValueError) as exc:
        signal = dict(
            date=prev, asof=cutoff.isoformat(), issue=str(exc), candidates=[], credit_complete=False
        )
    stopped = (root / "STOP").exists()
    raw = signal["candidates"]
    orders = {}
    for key in ACCOUNTS[:2]:
        eligible = [r for r in raw if not r["plus3_exclusions"]] if key.endswith("plus3") else raw
        orders[key] = account_orders(accounts[key], eligible, day, not stopped)
    stamp = datetime.now(JST)
    if stamp.date().isoformat() != day or stamp.time() >= time(9):
        return "missed_preopen"
    bundle = dict(
        target=day,
        exit_if_bought=sessions[i + 6],
        recorded_at=stamp.isoformat(),
        stopped=stopped,
        signal=signal,
        accounts_before=accounts,
        machine=orders,
    )
    # Keep the reviewer's input separate from the mechanical decisions.
    review = dict(
        target=day,
        signal_date=prev,
        asof=cutoff.isoformat(),
        stopped=stopped,
        exit_if_bought=bundle["exit_if_bought"],
        credit_refs=signal.get("credit_refs", []),
        input_refs=signal.get("input_refs", []),
        accounts={k: accounts[k] for k in ACCOUNTS[2:]},
        input_sha256=signature(bundle),
    )
    write(folder / "review-input.json", review)
    if datetime.now(JST) >= datetime.combine(date.fromisoformat(day), time(9), JST):
        return "missed_preopen"
    write(target, bundle)
    return "frozen"


def record_agent(root, day, answer):
    folder = root / "sessions" / day
    target = folder / "agent.json"
    if target.exists():
        return "already_frozen"
    pre = read(folder / "preopen.json")
    if (
        answer["target"] != day
        or answer["input_sha256"] != signature(pre)
        or set(answer["groups"]) != {"current", "plus3"}
    ):
        raise ValueError("agent_input_mismatch")
    codes = {r["code"]: r for r in pre["signal"]["candidates"]}
    diffs = {}
    executable = {}
    for arm, g in answer["groups"].items():
        buys, sells = g["buy"], g["sell"]
        state = pre["accounts_before"]["agent_" + arm]
        if not isinstance(g["reason"], str) or not g["reason"].strip():
            raise ValueError("missing_agent_reason")
        if not isinstance(g.get("candidate_notes"), dict) or not all(
            isinstance(note, str) and note.strip() for note in g["candidate_notes"].values()
        ):
            raise ValueError("missing_candidate_notes")
        if not all(
            isinstance(x, list) and all(isinstance(c, str) for c in x) and len(set(x)) == len(x)
            for x in (buys, sells)
        ):
            raise ValueError("invalid_agent_codes")
        if len(buys) > 5 or not all(
            len(c) == 5 and c.isascii() and c.isalnum() for c in buys + sells
        ):
            raise ValueError("invalid_agent_codes")
        if set(buys + sells) - g["candidate_notes"].keys():
            raise ValueError("missing_trade_reason")
        blocked = pre["stopped"] or (root / "STOP").exists() or state["status"] != "ok"
        eligible = [
            r["code"] for r in codes.values() if arm == "current" or not r["plus3_exclusions"]
        ][:5]
        cash = Decimal(state["cash"])
        accepted = {"buy": [], "sell": [], "skips": {}}
        for c in sells:
            if not blocked and c in state["positions"] and state["positions"][c]["exit"] <= day:
                accepted["sell"].append(c)
            else:
                accepted["skips"]["sell:" + c] = "blocked_or_not_due"
        for c in buys:
            if blocked or c in state["positions"] or c not in eligible:
                accepted["skips"]["buy:" + c] = "blocked_held_or_outside_frozen_rule"
            elif Decimal(codes[c]["estimated_debit"]) > cash:
                accepted["skips"]["buy:" + c] = "estimated_cash_short"
            else:
                cash -= Decimal(codes[c]["estimated_debit"])
                accepted["buy"].append(c)
        executable[arm] = accepted
        machine = pre["machine"]["machine_" + arm]
        diffs[arm] = {
            k: {"machine": machine[k], "agent": g[k], "same_order": machine[k] == g[k]}
            for k in ("buy", "sell")
        }
    stamp = datetime.now(JST)
    if stamp.date().isoformat() != day or stamp.time() >= time(9):
        raise ValueError("missed_agent_deadline")
    write(
        target,
        dict(
            recorded_at=stamp.isoformat(), answer=answer, executable=executable, differences=diffs
        ),
    )
    return "frozen"


def settle_account(previous, orders, quotes, day, due):
    from copy import deepcopy

    state = deepcopy(previous)
    if state["status"] != "ok":
        return state, [], None
    positions = state["positions"]
    cash = Decimal(state["cash"])
    fills = []

    def fill(code, action, kind, **kwargs):
        fills.append(dict(code=code, action=action, status=kind, **kwargs))

    for c in positions:
        bar = quotes.get(c)
        if not bar or money(bar["AdjFactor"]) != 1 or bar["ExRT"] not in ("", None):
            state.update(status="needs_review", reason=f"{day}: {c} holding right or missing row")
            return state, [], None
    fees = Decimal(state["fees"])
    slip = Decimal(state["slippage"])
    realized = Decimal(state["realized"])
    for side in ("sell", "buy"):
        for code in orders[side]:
            px = money(quotes.get(code, {}).get("O"))
            if px is None or px <= 0:
                fill(code, side, "missing_open")
                continue
            value = px * LOT
            debit = cost(px)
            if side == "buy":
                if debit > cash:
                    fill(code, side, "cash_short")
                    continue
                cash -= debit
                positions[code] = dict(entry=day, exit=due, debit=str(debit), quantity=LOT)
            else:
                credit = value * (1 - SLIP) - FEE
                position = positions.pop(code)
                cash += credit
                realized += credit - Decimal(position["debit"])
            fees += FEE
            slip += value * SLIP
            fill(
                code,
                side,
                "simulated",
                quantity=LOT,
                open=str(px),
                fee=str(FEE),
                slippage=str(value * SLIP),
            )
    state.update(cash=str(cash), realized=str(realized), fees=str(fees), slippage=str(slip))
    marks = {c: money(quotes.get(c, {}).get("C")) for c in positions}
    if any(v is None or v <= 0 for v in marks.values()):
        return state, fills, None
    nav = cash + sum(v * LOT for v in marks.values())
    high = max(Decimal(state["high"]), nav)
    dd = max(Decimal(state["max_dd"]), 1 - nav / high)
    state.update(high=str(high), max_dd=str(dd))
    unrealized = nav - CAPITAL - realized
    return (
        state,
        fills,
        dict(
            nav=str(nav),
            net=str(nav - CAPITAL),
            realized=str(realized),
            unrealized=str(unrealized),
            return_pct=str((nav / CAPITAL - 1) * 100),
            max_dd_pct=str(dd * 100),
            fees=str(fees),
            slippage=str(slip),
        ),
    )


def closing(project, root, day, sessions, now):
    folder = root / "sessions" / day
    if (folder / "close.json").exists():
        return "already_settled"
    i = sessions.index(day)
    accounts = get_books(root, sessions[i - 1])
    cols, _, sources = dated_inputs(project / "data/jquants", [day], now)
    quotes = {c: r[0] for c, r in cols.items()}
    pre = read(folder / "preopen.json") if (folder / "preopen.json").exists() else None
    agent = read(folder / "agent.json") if (folder / "agent.json").exists() else None
    result = {}
    fills = {}
    metrics = {}
    coverage = {}
    for key, account in accounts.items():
        engine, arm = key.split("_")
        orders = None
        if pre and engine == "machine":
            orders = pre["machine"][key]
        if pre and agent and engine == "agent":
            orders = agent["executable"][arm]
        if pre and pre["accounts_before"][key] != account:
            raise ValueError("stale_starting_account")
        if pre and pre["stopped"]:
            status = "stopped"
        elif orders is None:
            status = "missed_preopen"
        elif "issue" in pre["signal"]:
            status = "missing_signal"
        elif arm == "plus3" and not pre["signal"]["credit_complete"]:
            status = "credit_missing"
        else:
            status = "recorded"
        coverage[key] = status
        result[key], fills[key], metrics[key] = settle_account(
            account, orders or {"buy": [], "sell": []}, quotes, day, sessions[i + 6]
        )
        if result[key]["status"] != "ok" or metrics[key] is None:
            coverage[key] = "needs_review"
    diffs = {}
    for arm in ("current", "plus3"):
        a, m = metrics["agent_" + arm], metrics["machine_" + arm]
        diffs[arm] = dict(
            net_difference=str(Decimal(a["net"]) - Decimal(m["net"])) if a and m else None,
            both_decisions_recorded=all(
                coverage[k + "_" + arm] == "recorded" for k in ("agent", "machine")
            ),
        )
    for engine in ("machine", "agent"):
        a, b = metrics[engine + "_current"], metrics[engine + "_plus3"]
        diffs[engine + "_rules"] = dict(
            plus3_minus_current=str(Decimal(b["net"]) - Decimal(a["net"])) if a and b else None
        )
    write(
        folder / "close.json",
        dict(
            target=day,
            recorded_at=datetime.now(JST).isoformat(),
            accounts=result,
            coverage=coverage,
            fills=fills,
            metrics=metrics,
            comparison=diffs,
            sources=sources,
        ),
    )
    return "settled"


def run(project, action, answer=None):
    now = datetime.now(JST)
    root = project / "data/swing-forward"
    day = now.date().isoformat()
    with single_writer(root):
        if not (root / "initial.json").exists():
            write(root / "initial.json", dict(start=START, accounts=initial()))
        if action == "init":
            return {"state": "initialized", "start": START}
        if action == "review":
            closes = sorted((root / "sessions").glob("*/close.json"))
            return save_review(root, closes[-1].parent.name if closes else day)
        sessions = calendar(project / "data/jquants", now.date(), now)
        if day not in sessions:
            return {"state": "exchange_closed"}
        i = sessions.index(day)
        if action == "evening":
            if now.time() < time(18, 30):
                raise ValueError("evening_data_not_due")
            # Settlement is independent of the credit source.
            try:
                settled = catch_up(project, root, day, now)
            except (FileNotFoundError, ValueError) as exc:
                settled = {"needs_review": str(exc)}
            credit = {}
            if day >= START or (i + 1 < len(sessions) and sessions[i + 1] == START):
                for d in sessions[i - 1 : i + 1]:
                    try:
                        credit[d] = capture(project, d)
                    except Exception as exc:
                        credit[d] = f"needs_review:{type(exc).__name__}:{exc}"
            result = dict(state="evening", settled=settled, credit=credit)
            if (root / "sessions" / day / "close.json").exists():
                report = save_review(root, day)
                result["review"] = {
                    "file": f"reviews/{day}.json",
                    "matched_credit": report["matched_credit"],
                }
            return result
        if day < START:
            return {"state": "before_start"}
        if action == "morning":
            if now.time() >= time(9):
                return {"state": "missed_preopen"}
            catch_up(project, root, sessions[i - 1], now)
            status = morning(project, root, day, sessions, now)
            out = {"state": status}
            if status in ("frozen", "already_frozen"):
                out["input_sha256"] = signature(read(root / "sessions" / day / "preopen.json"))
            return out
        if action == "record-agent":
            return {"state": record_agent(root, day, read(answer))}
        raise ValueError("unknown_action")


def catch_up(project, root, last, now):
    if last < START:
        return []
    previous = sorted(p.parent.name for p in (root / "sessions").glob("*/close.json"))
    begin = date.fromisoformat(previous[-1] if previous else START)
    final = date.fromisoformat(last)
    settled = []
    while begin <= final:
        sessions = calendar(project / "data/jquants", begin, now)
        through = min(final, begin + timedelta(days=7))
        for d in sessions:
            if str(begin) <= d <= str(through) and (not previous or d > previous[-1]):
                daily_sessions = calendar(project / "data/jquants", date.fromisoformat(d), now)
                settled.append((d, closing(project, root, d, daily_sessions, now)))
        begin = through + timedelta(days=1)
    return settled


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["init", "morning", "record-agent", "evening", "review"])
    p.add_argument("--answer", type=Path)
    args = p.parse_args()
    result = run(Path.cwd(), args.action, args.answer)
    print(canonical(result))
    return int(
        isinstance(result.get("settled"), dict)
        or any(str(v).startswith("needs_review:") for v in result.get("credit", {}).values())
    )


if __name__ == "__main__":
    raise SystemExit(main())
