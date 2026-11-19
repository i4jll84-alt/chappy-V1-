"""Compare time extensions and one-day capped limits on archived swing stock baskets."""

import argparse
import importlib.util
import json
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal as D
from pathlib import Path

import duckdb
import pyarrow as pa

from trading_system.research.swing_daily import verify_pages

MODES = ("guarded", "extend_ma5", "extend_prior", "limit_gu3")
EXTENSIONS = MODES[1:3]


def base_module(project):
    path = project / "scripts/swing_gap_atr/replay_episodes.py"
    spec = importlib.util.spec_from_file_location("swing_gap_atr_v1", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_market(project, origin, picks, schedules, through, base):
    # Reuse verified calendar and OHLC used for every reference buy/ATR/exit.
    days, indices, market, checked = base.load_bars(project, origin, picks, schedules)
    extra = set()
    for (d, _), names in picks.items():
        i = indices[d]
        assert i + 20 < len(days)
        for name in names:
            extra.update(
                (t, name) for t in days[i + 8 : i + 21] if t <= through and (t, name) not in market
            )
    hist = project / "data/jquants/history"
    with sqlite3.connect(
        (hist / "history.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True
    ) as db:
        files = [
            hist / r[0]
            for r in db.execute(
                """select parquet_path from bulk_files where endpoint='/equities/bars/daily'
               and state='complete' and max_date>=? and min_date<=?""",
                (min(schedules), max(schedules)),
            )
        ]
    assert all(str(p.relative_to(project)) in checked for p in files)
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        db.from_parquet([str(p) for p in files]).create_view("bars")
        t, c = zip(*sorted(extra))
        db.register("needed", pa.table(dict(day=t, code=c)))
        rows = db.execute(
            """SELECT b.Date,b.Code,b.O,b.H,b.L,b.C,b.AdjFactor,b.ExRT FROM bars b
               JOIN needed n ON b.Date=n.day AND b.Code=n.code WHERE b.Date<=?""",
            [max(schedules)],
        ).fetchall()
        for d, code, *vals in rows:
            assert (d, code) not in market
            market[d, code] = base.bar(vals)
    for t in sorted({d for d, _ in extra if d > max(schedules)}):
        root = project / "data/jquants/daily-snapshots" / t
        receipt_path = root / "last_collection.json"
        if not receipt_path.is_file():
            continue
        receipt = json.loads(receipt_path.read_bytes())
        if receipt["state"] != "complete":
            continue
        assert receipt["date"] == t
        manifest_path = Path(receipt["daily_manifest"]).resolve()
        assert manifest_path.is_relative_to(root)
        meta = json.loads(manifest_path.read_bytes())
        count, _ = verify_pages(manifest_path.parent, meta, t)
        assert meta["state"] == "verified" and count == meta["rows"] == receipt["daily_rows"]
        for p in (receipt_path, manifest_path):
            checked[str(p.relative_to(project))] = base.sha(p)
        for page in meta["pages"]:
            p = manifest_path.parent / page["file"]
            checked[str(p.relative_to(project))] = base.sha(p)
            assert checked[str(p.relative_to(project))] == page["sha256"]
            for q in json.loads(p.read_bytes())["data"]:
                key = t, q["Code"]
                if key in extra:
                    assert key not in market
                    market[key] = base.bar([q.get(f) for f in base.FIELDS])
    return days, indices, market, checked


def ma5(days, j, code, market, base):
    bars = [market.get((days[k], code), base.EMPTY) for k in range(j - 4, j + 1)]
    # The first close is already expressed in that day's unit. Reject any unit
    # changes between the first close and the other closes.
    if any(not q[4] for q in bars[1:]) or any(not base.positive(q[3]) for q in bars):
        return None
    return sum(q[3] for q in bars) / 5


def buy_order(d, rule, rank, code, days, i, market, base):
    entry = days[i + 1]
    ref = market.get((d, code), base.EMPTY)[3]
    opening, _, low, close, clean, factor, ex = market.get((entry, code), base.EMPTY)
    atr = base.average_true_range(days, i, market, code)
    event = base.CONFIRMED_SPLITS.get((entry, code))
    if not clean and event and event["notice_date"] <= d:
        ratio = D(event["shares_after_per_share_before"])
        if ex == "1" and base.positive(factor) and abs(factor * ratio - 1) < D("1e-12"):
            ref = ref / ratio if base.positive(ref) else None
            atr = atr / ratio if atr is not None else None
            clean = True
    cap = ref * D("1.03") if base.positive(ref) else None
    row = dict(
        date=d,
        rule=rule,
        rank=rank,
        code=code,
        entry_day=entry,
        buy=None,
        entry_type=None,
        open_on_entry=opening,
        reference_close=ref,
        cap=cap,
        atr14=atr,
        stop=None,
        extended=False,
        due_day=days[i + 7],
        due_open=None,
        due_prior_close=None,
        due_prior_ma5=None,
        eligible_due_open=None,
        eligible_prior_close=None,
        holding_sessions=None,
        exit_type=None,
        exit_day=None,
        exit_price=None,
        entry_fee=None,
        entry_slippage=None,
        exit_fee=None,
        exit_slippage=None,
        net=None,
        issue=None,
    )
    if not base.positive(opening):
        row["atr14"] = None
        row["exit_type"] = "skip_open"
    elif not clean or cap is None:
        row["exit_type"] = "unresolved"
        row["issue"] = entry + "/incomparable_open_or_reference"
    elif opening > cap and rule != "limit_gu3":
        row["exit_type"] = "skip_gap"
    elif atr is None:
        row["exit_type"] = "skip_atr"
    elif opening > cap and (
        not base.positive(low) or low > opening or (base.positive(close) and low > close)
    ):
        row["exit_type"] = "unresolved"
        row["issue"] = entry + "/limit_low_missing_or_invalid"
    elif opening > cap and low > cap:
        row["exit_type"] = "skip_limit"
    else:
        px = min(opening, cap)
        row.update(buy=px, entry_type="limit" if opening > cap else "open", stop=px - 2 * atr)
    return row


def day_event(rule, row, day, j, i, days, market, through, base, pending):
    """Return reason to exit, modeled price, and reason for an unknown position."""
    code = row["code"]
    opening, _, low, close, clean, _, _ = market.get((day, code), base.EMPTY)
    if day > through:
        return None, None, "beyond_price_cutoff"
    if (day, code) not in market:
        return None, None, "held_daily_bar_missing"
    if j > i + 1 and not clean:
        return None, None, "held_corporate_action"
    if not base.positive(opening):
        return None, None, "held_open_missing"
    if j == i + 7:
        if rule not in EXTENSIONS:
            return "scheduled", opening, None
        prev_close = market.get((days[j - 1], code), base.EMPTY)[3]
        average = ma5(days, j - 1, code, market, base)
        if average is None or not base.positive(prev_close):
            return None, None, "missing_extension_decision"
        row.update(
            due_open=opening,
            due_prior_close=prev_close,
            due_prior_ma5=average,
            eligible_due_open=opening > row["buy"] and prev_close > average,
            eligible_prior_close=prev_close > row["buy"] and prev_close > average,
        )
        row["extended"] = row[
            "eligible_due_open" if rule == "extend_ma5" else "eligible_prior_close"
        ]
        if not row["extended"]:
            return "scheduled", opening, None
    if pending:
        return "ext_ma5_break", opening, None
    if j == i + 20:
        return "ext_max_days", opening, None
    if j > i + 1 and base.positive(row["stop"]) and opening <= row["stop"]:
        return "stop_open", opening, None
    if not base.positive(low) or low > opening or (base.positive(close) and low > close):
        return None, None, "held_low_missing_or_invalid"
    if base.positive(row["stop"]) and low <= row["stop"]:
        return "stop_touch", row["stop"], None
    if not base.positive(close):
        return None, None, "mark_missing"
    if row["extended"]:
        average = ma5(days, j, code, market, base)
        if average is None:
            return None, None, "missing_extended_ma5"
        if close < average:
            return "pending", None, None
    return None, None, None


def basket(day, rule, names, days, i, market, through, base):
    cash = peak = base.CAPITAL
    issues = []
    actions = []
    positions = {}
    pending = set()
    marks = []
    worst = D(0)
    for j in range(i + 1, i + (21 if rule in EXTENSIONS else 8)):
        t = days[j]
        if j == i + 1:
            for rank, code in enumerate(names, 1):
                row = buy_order(day, rule, rank, code, days, i, market, base)
                actions.append(row)
                if row["issue"]:
                    issues.append(code + "/" + row["issue"])
                if row["buy"] is None:
                    continue
                gross = row["buy"] * base.SHARES
                debit = gross * (1 + base.SLIP) + base.FEE
                if debit > cash:
                    row.update(buy=None, stop=None, entry_type=None, exit_type="skip_cash")
                    continue
                cash -= debit
                row.update(entry_fee=base.FEE, entry_slippage=gross * base.SLIP)
                positions[code] = row
        nav = cash
        for code, row in list(positions.items()):
            exit_type, price, issue = day_event(
                rule, row, t, j, i, days, market, through, base, code in pending
            )
            if issue:
                row.update(exit_type="unresolved", issue=t + "/" + issue)
                issues.append(code + "/" + row["issue"])
                del positions[code]
                continue
            if exit_type is None or exit_type == "pending":
                nav += market[t, code][3] * base.SHARES
                if exit_type:
                    pending.add(code)
                continue
            gross = price * base.SHARES
            proceeds = gross * (1 - base.SLIP) - base.FEE
            cash += proceeds
            nav += proceeds
            row.update(
                exit_type=exit_type,
                exit_day=t,
                exit_price=price,
                exit_fee=base.FEE,
                exit_slippage=gross * base.SLIP,
                holding_sessions=j - i - 1,
                net=proceeds - (row["buy"] * base.SHARES * (1 + base.SLIP) + base.FEE),
            )
            del positions[code]
        if not issues:
            peak = max(peak, nav)
            worst = max(worst, 1 - nav / peak)
        marks.append(
            dict(
                date=day,
                rule=rule,
                session=t,
                cash=cash if not issues else None,
                nav=nav if not issues else None,
                dd_pct=(1 - nav / peak) * 100 if not issues else None,
                positions=len(positions),
                status="unresolved" if issues else "ok",
            )
        )
        if j >= i + 7 and not positions:
            break
    assert not positions
    if not issues:
        assert abs(sum(r["net"] or D(0) for r in actions) - (cash - base.CAPITAL)) < D("1e-10")
    counts = Counter(r["exit_type"] for r in actions)
    exits = ("scheduled", "stop_touch", "stop_open", "ext_ma5_break", "ext_max_days")
    rec = dict(
        date=day,
        rule=rule,
        status="unresolved" if issues else "ok",
        net=cash - base.CAPITAL if not issues else None,
        dd_pct=worst * 100 if not issues else None,
        candidates=len(names),
        buys=sum(r["buy"] is not None for r in actions),
        skips=sum(v for k, v in counts.items() if k.startswith("skip_")),
        open_fills=sum(r["entry_type"] == "open" for r in actions),
        limit_fills=sum(r["entry_type"] == "limit" for r in actions),
        gap_skips=counts["skip_gap"],
        limit_skips=counts["skip_limit"],
        eligible_due_open=sum(r["eligible_due_open"] is True for r in actions),
        eligible_prior_close=sum(r["eligible_prior_close"] is True for r in actions),
        extended=sum(r["extended"] for r in actions),
        **{k: counts[k] for k in exits},
        fees=sum((r["entry_fee"] or 0) + (r["exit_fee"] or 0) for r in actions),
        slippage=sum((r["entry_slippage"] or 0) + (r["exit_slippage"] or 0) for r in actions),
        issues=";".join(issues),
    )
    return rec, actions, marks


def run(project, prior, out, through):
    project, prior, out = project.resolve(), prior.resolve(), out.resolve()
    out.mkdir(exist_ok=True, parents=True)
    base = base_module(project)
    origin = project / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs"
    saved = base.read_csv(prior / "paired.csv")
    common = [r["date"] for r in saved if all(r[k + "_status"] == "ok" for k in base.RULES)]
    assert len(common) == len(set(common)) == 709 and len(saved) == 730
    rankings = defaultdict(list)
    for r in base.read_csv(origin / "selection.csv"):
        if r["decision"] in common and r["arm"] == "new":
            names = rankings[r["decision"], "new"]
            assert int(r["rank"]) == len(names) + 1 and r["code"] not in names
            names.append(r["code"])
    originals = {
        (r["date"], r["code"]): r
        for r in base.read_csv(prior / "orders.csv")
        if r["rule"] == "guarded" and r["date"] in common
    }
    ref = {r["date"]: r for r in base.read_csv(prior / "windows.csv") if r["rule"] == "guarded"}
    schedules = {
        r["decision"]: r
        for r in base.read_csv(origin / "coverage.csv")
        if r["reference"] == "False"
    }
    days, indices, market, checked = load_market(
        project, origin, rankings, schedules, through, base
    )
    all_rows = []
    all_marks = []
    all_actions = []
    reconciled = set()
    for d in common:
        names = rankings[d, "new"]
        for rule in MODES:
            rec, actions, marks = basket(d, rule, names, days, indices[d], market, through, base)
            if rule == "guarded":
                old = ref[d]
                assert rec["status"] == old["status"] == "ok", (d, rec, old)
                for k in (
                    "net",
                    "dd_pct",
                    "buys",
                    "skips",
                    "stop_touch",
                    "stop_open",
                    "scheduled",
                    "fees",
                    "slippage",
                ):
                    assert rec[k] == D(old[k]), (d, k, rec[k], old[k])
                for a in actions:
                    key = d, a["code"]
                    prev = originals[key]
                    assert a["exit_type"] == prev["exit_type"]
                    assert (a["exit_day"] or "") == prev["exit_day"]
                    for k in ("buy", "net", "atr14", "stop", "exit_price"):
                        assert a[k] == (D(prev[k]) if prev[k] else None), (key, k)
                    reconciled.add(key)
            all_rows.append(rec)
            all_marks.extend(marks)
            all_actions.extend(actions)
    assert set(originals) == reconciled
    for name, records in (
        ("windows.csv", all_rows),
        ("orders.csv", all_actions),
        ("nav.csv", all_marks),
    ):
        base.write_csv(out / name, records)
    sample = {
        mode: dict(Counter(r["status"] for r in all_rows if r["rule"] == mode)) for mode in MODES
    }
    evidence = dict(
        price_cutoff=through,
        dates=len(common),
        baseline_windows_verified=len(common),
        known_delisting=dict(
            code="37870",
            last_trading_day="2026-09-30",
            delisting_date="2026-10-01",
            announced="2026-06-03",
            source="https://www.jpx.co.jp/news/1021/20260603-23.html",
            handling="Held position unresolved with no observable post-delisting exchange exit",
        ),
        baseline_orders_verified=len(reconciled),
        counts=sample,
        sources=checked,
        previous_archived_hashes={
            p: base.sha(prior / p) for p in ("windows.csv", "paired.csv", "orders.csv")
        },
        selection_sha256=base.sha(origin / "selection.csv"),
    )
    (out / "verification.json").write_text(
        json.dumps(evidence, sort_keys=True, indent=2), encoding="utf-8"
    )
    print(json.dumps(sample))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", required=True, type=Path)
    p.add_argument("--prior", type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--through", default="2026-11-18")
    a = p.parse_args()
    run(
        a.project,
        a.prior or a.project / "data/offline-verification/swing-gap-atr-20261118/episodes",
        a.out,
        a.through,
    )
