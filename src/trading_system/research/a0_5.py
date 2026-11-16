"""A0_5 順張りブレイクアウト. Local, opt-in paper account only."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from trading_system.brokers.locking import single_writer
from trading_system.storage.store import atomic_write

from . import a0_5_book as book
from . import a0_5_inputs as inputs
from .a0_5_rules.config import A0_5
from .a0_5_rules.data_fetcher import iso
from .a0_5_rules.live_portfolio import code_of
from .a0_5_rules.live_screen import LIVE_COLUMNS, plan
from .a0_5_rules.strategy import Position, ranked_candidates, sector_code
from .a0_5_rules.strategy import orders as fixed_orders
from .daytrade_universe import dated_inputs

NAME = "A0_5 順張りブレイクアウト"
MODE = "運用ガード付きペーパー"
JST = ZoneInfo("Asia/Tokyo")


def root_for(project):
    return project / "data/a0-5-paper"


def enabled(project):
    return (root_for(project) / "enabled").is_file()


def stopped(project):
    return any(
        (project / folder / "STOP").exists() for folder in ("data/a0-5-paper", "data/swing-forward")
    )


def select(project, active):
    root = root_for(project)
    with single_writer(root):
        flag = root / "enabled"
        if active and not flag.exists():
            atomic_write(flag, datetime.now(JST).isoformat().encode())
        elif not active and flag.exists():
            s = book.load(root)
            for entry in s["plans"].values():
                if entry.get("state") == "ready" and entry["order_date"] not in s["openings"]:
                    entry["state"] = "disabled"
            book.save(root, s)
            flag.unlink()


def manual_schedule(project, code, scheduled, source, now):
    day = now.date().isoformat()
    code = code_of(code)
    scheduled = "" if scheduled == "none" else iso(pd.Timestamp(scheduled))
    if not source.strip() or (scheduled and scheduled <= day):
        raise ValueError("確認した原本と将来の決算日（期間内予定なしは none）を指定してください")
    root = root_for(project)
    with single_writer(root):
        s = book.load(root)
        s["manual"].setdefault(day, {})[code] = dict(
            scheduled_date=scheduled, source=source.strip(), received_at=now.isoformat()
        )
        book.save(root, s)


def assess(project, day, now, s):
    if day.date() != now.astimezone(JST).date():
        raise ValueError("過去日の引け判定は保存済みの版を参照してください")
    history, cal, counts, refs = inputs.read_inputs(project, day, now)
    ranked = ranked_candidates(history)
    codes = set(ranked.Code) | {p["Code"] for p in s["positions"]}
    schedule, schedule_ref = inputs.schedules(project, codes, day, now, s["manual"])
    actions = {
        r.Code: "分割・併合等を確認。新規は1立会日見送り推奨"
        for r in history.itertuples()
        if pd.notna(r.AdjFactor) and r.AdjFactor != 1
    }
    orders, notes, summary = plan(history, day, cal, s, schedule, actions)
    # Compare both order builders on the same close, holdings and input version.
    at_close = history.set_index("Code")
    positions = {
        p["Code"]: Position(
            p["shares"],
            pd.Timestamp(p["entry_date"]),
            p["cost"],
            float(at_close.at[p["Code"], "C"]),
            p.get("pending_exit", ""),
            p["S33"],
        )
        for p in s["positions"]
    }
    fixed = fixed_orders(history, day, cal, s["cash"], positions, A0_5)
    comparison = []
    for side, code in sorted(set(zip(fixed.side, fixed.Code)) | set(zip(orders.side, orders.Code))):
        lhs = fixed.loc[fixed.side.eq(side) & fixed.Code.eq(code)]
        rhs = orders.loc[orders.side.eq(side) & orders.Code.eq(code)]
        before = int(lhs.shares.iloc[0]) if not lhs.empty else 0
        after = int(rhs.shares.iloc[0]) if not rhs.empty else 0
        reasons = []
        if not rhs.empty and rhs.reason.iloc[0] == "EARNINGS_AVOID":
            reasons.append("EARNINGS_AVOID")
        if before != after:
            if side == "BUY" and code in notes:
                reasons.append(notes[code]["decision"])
            if summary["locked"]:
                reasons.append("未受渡拘束あり")
            if not rhs.empty and rhs.conditional_on_sales.iloc[0]:
                reasons.append("売り成立条件付き")
        if reasons or before != after:
            comparison.append(
                dict(side=side, Code=code, fixed_shares=before, paper_shares=after, reasons=reasons)
            )
    versions = [
        "Code",
        "FinDate",
        "FinEnd",
        "MarginDate",
        "MarginEnd",
        "MarginPubDate",
        "is_published_date_estimated",
        "FlowDate",
        "FlowEnd",
        "C",
        "S33",
        "AvgTradingValue20",
        "PriorHigh",
        "VolumeRatio",
        "MarginDays",
        "SectorReturn",
        "IndexReturn",
        "SectorShare",
        "PriorShare",
        "Sales",
        "PriorSales",
        "OP",
        "PriorOP",
        "FrgnBal",
        "S17",
    ]
    candidates = json.loads(
        ranked[versions].to_json(orient="records", date_format="iso", double_precision=15)
    )
    for rank, row in enumerate(candidates, 1):
        row.update(rank=rank, **notes[row["Code"]])
    return dict(
        state="ready",
        mode=MODE,
        date=iso(day),
        order_date=iso(cal[cal.get_loc(day) + 1]),
        cutoff=now.isoformat(),
        counts=counts,
        inputs=refs,
        earnings_input=schedule_ref,
        candidates=candidates,
        holding_sectors={
            c: sector_code(at_close.at[c, "S33"]) or p.sector33 for c, p in positions.items()
        },
        guard_differences=comparison,
        **summary,
        orders=json.loads(orders.to_json(orient="records", double_precision=15)),
    )


def export(root, record, revision):
    folder = root / "sessions" / record["date"] / f"v{revision:06d}"
    decision = folder / "decision.json"
    if decision.exists():
        if json.loads(decision.read_bytes()) != record:
            raise ValueError(f"台帳と保存済み判定が不一致 {decision}")
    else:
        atomic_write(
            decision,
            json.dumps(
                record, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
            ).encode(),
        )
    if record["state"] == "ready":
        path = folder / "plan.csv"
        expected = pd.DataFrame(record["orders"], columns=LIVE_COLUMNS).to_csv(index=False)
        if path.exists():
            if path.read_bytes() != expected.encode("utf-8-sig"):
                raise ValueError(f"台帳と保存済み予定が不一致 {path}")
        else:
            atomic_write(path, expected.encode("utf-8-sig"))


def mark_stop(project, day, now):
    # The existing V1 stop can be lifted before an official opening price arrives.
    root = root_for(project)
    if enabled(project) and now.astimezone(JST).hour >= 9:
        atomic_write(
            root / "stops" / (iso(day) + ".json"),
            json.dumps({"observed_at": now.isoformat()}).encode(),
        )


def opening(project, now=None):
    now = now or datetime.now(JST)
    day = pd.Timestamp(now.astimezone(JST).date())
    if not enabled(project):
        return {"state": "off"}
    if stopped(project):
        mark_stop(project, day, now)
        return {"state": "stopped"}
    if now.astimezone(JST).hour < 9:
        return {"state": "before_open"}
    root = root_for(project)
    with single_writer(root):
        if not enabled(project) or stopped(project):
            return {"state": "stopped"}
        try:
            s = apply_openings(project, book.load(root), day, now)
        except FileNotFoundError:
            return {"state": "waiting_for_verified_open", "date": iso(day)}
        return {"state": "reconciled", "date": iso(day), "last_open": s["last_open"]}


def apply_openings(project, s, through, now):
    root = root_for(project)
    todo = {
        r["order_date"]
        for r in s["plans"].values()
        if r["state"] == "ready"
        and r["order_date"] <= iso(through)
        and r["order_date"] not in s["openings"]
    }
    last = s["last_open"]
    if (s["positions"] and last) or todo:
        # Check corporate actions on every intervening session, including sessions without orders.
        first = last if s["positions"] and last else min(todo)
        pivot = pd.Timestamp(first)
        while pivot <= through:
            cal = inputs.calendar(project, pivot, now, [])
            todo.update(iso(d) for d in cal if first < iso(d) <= iso(through))
            pivot += pd.Timedelta(days=21)
    for date in sorted(todo):
        if stopped(project):
            raise ValueError("ペーパー停止中")
        cal = inputs.calendar(project, pd.Timestamp(date), now, [])
        plans = [
            r for r in s["plans"].values() if r["state"] == "ready" and r["order_date"] == date
        ]
        orders = [o for r in plans for o in r["orders"]]
        if not plans and not s["positions"]:
            continue
        if orders or s["positions"]:
            cols, _, refs = dated_inputs(project / "data/jquants", [date], now)
            prices = {
                c: {k: rows[0].get(k) for k in ("O", "AdjFactor", "ExRT")}
                for c, rows in cols.items()
            }
        else:
            prices, refs = {}, []
        pre = project / "data/swing-forward/sessions" / date / "preopen.json"
        skipped = (root / "stops" / (date + ".json")).is_file() or (
            pre.is_file() and json.loads(pre.read_bytes()).get("stopped") is True
        )
        if skipped:
            orders = [dict(o, stop_observed=True) for o in orders]
        s = book.execute(s, date, orders, prices, cal, refs)
        if stopped(project):
            raise ValueError("ペーパー停止中")
        book.save(root, s)
    return s


def run(project, date=None, *, now=None, preview=False, refresh=False, revise=False):
    now = now or datetime.now(JST)
    day = pd.Timestamp(date or now.astimezone(JST).date())
    if not preview and not enabled(project):
        return {"state": "off", "mode": MODE}
    if stopped(project):
        if not preview and day.date() == now.astimezone(JST).date():
            mark_stop(project, day, now)
        return {"state": "stopped", "mode": MODE}
    if preview:
        if refresh:
            inputs.refresh_flow(project, now)
            now = datetime.now(JST)
        s = book.load(root_for(project))
        return assess(project, day, now, s)
    root = root_for(project)
    with single_writer(root):
        if not enabled(project) or stopped(project):
            return {"state": "stopped", "mode": MODE}
        s = book.load(root)
        if not s["started_at"]:
            s["started_at"] = (root / "enabled").read_text()
            book.save(root, s)
        # A late receipt can settle a frozen order; only today can get a new decision.
        if day > pd.Timestamp(now.astimezone(JST).date()):
            raise ValueError("将来日の評価")
        s = apply_openings(project, s, day, now)
        if iso(day) != now.astimezone(JST).date().isoformat():
            return {"state": "reconciled", "date": iso(day), "mode": MODE}
        old = s["plans"].get(iso(day))
        if old and old["state"] == "ready" and not revise:
            export(root, old, old["saved_revision"])
            return old
        try:
            if refresh:
                inputs.refresh_flow(project, now)
                now = datetime.now(JST)
            record = assess(project, day, now, s)
        except Exception as exc:
            # Persist operational failures without exposing request URLs or credentials.
            reason = (
                str(exc)
                if isinstance(exc, (ValueError, KeyError, FileNotFoundError))
                else getattr(exc, "category", type(exc).__name__)
            )
            record = dict(
                state="blocked",
                mode=MODE,
                date=iso(day),
                cutoff=now.isoformat(),
                reason=reason,
                orders=[],
            )
        if stopped(project):
            return {"state": "stopped", "mode": MODE}
        for p in s["positions"]:
            p["S33"] = record.get("holding_sectors", {}).get(p["Code"], p["S33"])
        record["saved_revision"] = s["revision"] + 1
        s["plans"][iso(day)] = record
        book.save(root, s)
        export(root, record, s["revision"])
        return record


def after_daily(project, date):
    if not enabled(project):
        return None
    try:
        r = run(project, date, refresh=True)
        return {k: r[k] for k in ("state", "date", "order_date", "reason") if k in r}
    except Exception as exc:
        # A paper failure must not turn a successfully saved daily receipt into a failed collection.
        return {
            "state": "blocked",
            "reason": str(exc)
            if isinstance(exc, (ValueError, FileNotFoundError))
            else type(exc).__name__,
        }


def status(project, now=None):
    now = now or datetime.now(JST)
    s = book.load(root_for(project))
    active = enabled(project)
    stop = stopped(project)
    rec = s["plans"].get(now.astimezone(JST).date().isoformat()) if active and not stop else None
    today = now.astimezone(JST).date().isoformat()
    due = (
        next(
            (
                r
                for r in s["plans"].values()
                if r["state"] == "ready" and r["order_date"] == today and r["orders"]
            ),
            None,
        )
        if active and not stop and today not in s["openings"]
        else None
    )
    return dict(
        due=due,
        strategy=NAME,
        mode=MODE,
        enabled=active,
        stopped=stop,
        started_at=s["started_at"],
        capital=s["capital"],
        cash=s["cash"],
        positions=s["positions"],
        settlements=s["settlements"],
        decision=rec,
        audit=book.audit(s),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("status", "enable", "disable", "preview", "daily", "opening", "confirm")
    )
    parser.add_argument("--date")
    parser.add_argument("--refresh-flow", action="store_true")
    parser.add_argument("--revise", action="store_true", help="同日中の訂正版を別版として固定")
    parser.add_argument("--code")
    parser.add_argument("--scheduled")
    parser.add_argument("--source")
    args = parser.parse_args()
    project = Path.cwd()
    if args.action in ("enable", "disable"):
        select(project, args.action == "enable")
    if args.action == "confirm":
        if not all((args.code, args.scheduled, args.source)):
            parser.error("--code --scheduled --source are required")
        manual_schedule(project, args.code, args.scheduled, args.source, datetime.now(JST))
    if args.action == "opening":
        result = opening(project)
    elif args.action in ("preview", "daily"):
        result = run(
            project,
            args.date,
            preview=args.action == "preview",
            refresh=args.refresh_flow,
            revise=args.revise,
        )
    else:
        result = status(project)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
