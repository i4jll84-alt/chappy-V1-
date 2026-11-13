"""Review the existing prospective swing ledgers without altering orders or account state."""

import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

from trading_system.research.swing_earnings_review import EarningsReview
from trading_system.storage.store import atomic_write, canonical


def summarize(root: Path, through: str):
    """Pair by entry session. Both arms must be observable and all cohort buys must be closed."""
    date.fromisoformat(through)
    initial = json.loads((root / "initial.json").read_bytes())
    engines = ("machine", "agent")
    rules = ("current", "plus3")
    books = initial["accounts"]
    earnings = EarningsReview(books)
    fills = {key: Counter() for key in books}
    coverage = {key: Counter() for key in books}
    lots = {key: {} for key in books}
    cohorts = {engine: {} for engine in engines}
    latest_metrics = dict.fromkeys(books)
    last_session = None
    days = 0
    for file in sorted((root / "sessions").glob("*/close.json")):
        day = file.parent.name
        if not initial["start"] <= day <= through:
            continue
        close = json.loads(file.read_bytes())
        if close["target"] != day:
            raise ValueError("review_target_mismatch")
        prefile = file.parent / "preopen.json"
        pre = json.loads(prefile.read_bytes()) if prefile.is_file() else None
        agentfile = file.parent / "agent.json"
        agent = json.loads(agentfile.read_bytes()) if agentfile.is_file() else None
        earnings.update(close, pre, agent)
        if pre and pre["target"] != day:
            raise ValueError("review_preopen_mismatch")
        for engine in engines:
            if (
                pre
                and pre["signal"]["credit_complete"]
                and all(close["coverage"][f"{engine}_{rule}"] == "recorded" for rule in rules)
            ):
                cohorts[engine][day] = dict(
                    due=pre["exit_if_bought"],
                    net={r: Decimal(0) for r in rules},
                    buys={r: 0 for r in rules},
                    open=0,
                )
        for key, trades in close["fills"].items():
            engine, rule = key.split("_")
            coverage[key][close["coverage"][key]] += 1
            for fill in trades:
                if fill["status"] != "simulated":
                    continue
                c = fill["code"]
                value = Decimal(fill["open"]) * fill["quantity"]
                costs = Decimal(fill["fee"]) + Decimal(fill["slippage"])
                side = fill["action"]
                fills[key][side] += 1
                if side == "buy":
                    if c in lots[key]:
                        raise ValueError("review_duplicate_open_lot")
                    lots[key][c] = (day, value + costs)
                    if day in cohorts[engine]:
                        group = cohorts[engine][day]
                        group["open"] += 1
                        group["buys"][rule] += 1
                elif side == "sell":
                    entry, debit = lots[key].pop(c)
                    if entry in cohorts[engine]:
                        group = cohorts[engine][entry]
                        group["open"] -= 1
                        group["net"][rule] += value - costs - debit
                else:
                    raise ValueError("review_unknown_fill_side")
            if set(lots[key]) != set(close["accounts"][key]["positions"]):
                raise ValueError("review_open_positions_mismatch")
        books = close["accounts"]
        latest_metrics = close["metrics"]
        last_session = day
        days += 1
    accounts = {}
    for key, book in books.items():
        metric = latest_metrics[key]
        accounts[key] = dict(
            net_yen=metric["net"] if metric else None,
            realized_yen=book["realized"],
            unrealized_yen=metric["unrealized"] if metric else None,
            max_drawdown_pct=metric["max_dd_pct"] if metric else None,
            fees_yen=book["fees"],
            slippage_yen=book["slippage"],
            buys=fills[key]["buy"],
            sells=fills[key]["sell"],
            open_positions=len(book["positions"]),
            coverage=dict(coverage[key]),
        )
    matched = {}
    for engine, groups in cohorts.items():
        closed = [v for v in groups.values() if v["due"] <= last_session and not v["open"]]
        totals = {r: sum((v["net"][r] for v in closed), Decimal(0)) for r in rules}
        matched[engine] = dict(
            qualifying_entry_sessions=len(groups),
            completed_sessions=len(closed),
            pending_sessions=len(groups) - len(closed),
            completed_buys={r: sum(v["buys"][r] for v in closed) for r in rules},
            net_yen={r: str(totals[r]) if closed else None for r in rules},
            plus3_minus_current_yen=str(totals["plus3"] - totals["current"]) if closed else None,
        )
    return dict(
        start=initial["start"],
        through=through,
        last_settled=last_session,
        settled_sessions=days,
        accounts=accounts,
        matched_credit=matched,
        earnings_review=earnings.report(),
    )


def save_review(root, through):
    report = summarize(root, through)
    if report["settled_sessions"]:
        target = root / "reviews" / (through + ".json")
        atomic_write(target, canonical(report).encode(), sharing_retries=3)
    return report
