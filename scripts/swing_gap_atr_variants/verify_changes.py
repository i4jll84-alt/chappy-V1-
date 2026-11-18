"""Independent event-level audit of gap limits and optional MA5 extensions on frozen episodes."""

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

import duckdb
import pyarrow as pa

FEE = D(10)
SLIP = D("0.0005")
SHARES = 100
SUM_TOLERANCE = D("1e-12")


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def put_csv(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in records for k in r)))
        w.writeheader()
        w.writerows(records)


def json_read(path):
    return json.loads(path.read_bytes())


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def value(v):
    if v in (None, ""):
        return None
    p = D(str(v))
    return p if p > 0 else None


class Quotes:
    def __init__(self, project, orders):
        hist = project / "data/jquants/history"
        jquants = project / "data/jquants"
        sources = json_read(
            project / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs/sources.json"
        )
        assert (
            not (hist / "history.sqlite-wal").exists()
            or (hist / "history.sqlite-wal").stat().st_size == 0
        )
        with sqlite3.connect(
            (hist / "history.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True
        ) as db:
            parts = db.execute(
                "SELECT endpoint,parquet_path,parquet_sha256 FROM bulk_files WHERE "
                "endpoint IN ('/markets/calendar','/equities/bars/daily') AND state='complete' "
                "AND max_date>='2023-10-01' AND min_date<='2026-10-30'"
            ).fetchall()
        cal, bars = [], []
        for ep, name, digest in parts:
            p = hist / name
            assert sha(p) == digest == sources[str(p.relative_to(project)).replace("/", chr(92))]
            (bars if ep.endswith("daily") else cal).append(str(p))
        with duckdb.connect() as db:
            db.execute("SET threads=2")
            db.from_parquet(cal).create_view("cal")
            self.days = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT Date FROM cal WHERE HolDiv IN ('1','2') ORDER BY Date"
                ).fetchall()
            ]
            self.idx = {d: i for i, d in enumerate(self.days)}
            needed = set()
            for r in orders:
                day, code = r["date"], r["code"]
                i = self.idx[day]
                needed.update((s, code) for s in self.days[i : i + 21])
            dates, codes = zip(*sorted(needed))
            db.register("wanted", pa.table({"day": dates, "code": codes}))
            db.from_parquet(bars).create_view("bars")
            raw = db.execute(
                "SELECT b.Date,b.Code,b.O,b.H,b.L,b.C,b.AdjFactor,b.ExRT "
                "FROM bars b JOIN wanted w ON b.Date=w.day AND b.Code=w.code"
            ).fetchall()

        def decode(op, hi, low, close, factor, ex):
            return tuple(value(p) for p in (op, hi, low, close)), (
                value(factor) == 1 and ex in ("", None)
            )

        self.quotes = {(day, c): decode(*fields) for day, c, *fields in raw}
        assert len(raw) == len(self.quotes)
        for day in sorted({s for s, _ in needed if "2026-10-30" < s <= "2026-11-18"}):
            folder = jquants / "daily-snapshots" / day
            receipt = json_read(folder / "last_collection.json")
            manifest = Path(receipt["daily_manifest"]).resolve()
            assert (
                manifest.is_relative_to(folder.resolve())
                and receipt["date"] == day
                and receipt["state"] == "complete"
            )
            meta = json_read(manifest)
            assert meta["state"] == "verified"
            assert datetime.fromisoformat(receipt["completed_at"]) <= datetime.fromisoformat(
                day + "T23:59:59+09:00"
            )
            for page in meta["pages"]:
                p = manifest.parent / page["file"]
                key = str(p.relative_to(project)).replace("/", chr(92))
                assert page["sha256"] == sha(p)
                if key in sources:
                    assert sources[key] == page["sha256"]
                for q in json_read(p)["data"]:
                    c = q["Code"]
                    if (day, c) in needed:
                        assert (day, c) not in self.quotes
                        self.quotes[day, c] = decode(
                            *(q.get(k) for k in ("O", "H", "L", "C", "AdjFactor", "ExRT"))
                        )

    def session(self, day, code):
        return self.quotes.get((day, code), ((None, None, None, None), False))

    def close_ma(self, at, code):
        prices = []
        for s in self.days[at - 4 : at + 1]:
            (_, _, _, c), good = self.session(s, code)
            if not good or c is None:
                return None, None
            prices.append(c)
        return prices[-1], sum(prices) / 5


def pnl(buy, sell):
    return SHARES * sell * (1 - SLIP) - FEE - (SHARES * buy * (1 + SLIP) + FEE)


def sale(event, day, price, buy):
    return dict(result=event, final=day, sell=price, net=pnl(buy, price))


def skip(reason):
    return dict(result=reason, final=None, sell=None, net=D(0))


def unknown(issue):
    return dict(result="unresolved", issue=issue, final=None, sell=None, net=None)


def old_outcome(r):
    return dict(
        result=r["exit_type"],
        final=r["exit_day"] or None,
        sell=value(r["exit_price"]),
        net=D(r["net"] or 0),
    )


def stop_or_hold(quotes, code, stop, day, check_gap):
    (op, _, low, close), clean = quotes.session(day, code)
    if not clean or op is None:
        return unknown(day + "/right_or_open_missing")
    if check_gap and stop > 0 and op <= stop:
        return "stop_open", op
    if low is None or low > op or (close is not None and low > close):
        return unknown(day + "/low_missing_or_invalid")
    if stop > 0 and low <= stop:
        return "stop_touch", stop
    if close is None:
        return unknown(day + "/missing_close")
    return None


def limit_reprice(q, r):
    d, c = r["date"], r["code"]
    i = q.idx[d]
    (_, _, _, reference), _ = q.session(d, c)
    (op, _, low, close), clean = q.session(r["entry_day"], c)
    assert clean and reference is not None and op > reference * D("1.03")
    cap = reference * D("1.03")
    atr = value(r["atr14"])
    detail = dict(cap=cap, touch_only=(low == cap), entry_low=low, price=None, new_stop=None)
    if low is None or low > op or (close is not None and low > close):
        return {**detail, **unknown("missing_entry_low")}
    if low > cap:
        return {**detail, **skip("skip_limit")}
    if atr is None:
        return {**detail, **skip("skip_atr")}
    stop = cap - 2 * atr
    detail.update(price=cap, new_stop=stop)
    for n in range(1, 8):
        day = q.days[i + n]
        if n == 7:
            (op, _, _, _), ok = q.session(day, c)
            if not ok or op is None:
                return {**detail, **unknown(day + "/scheduled_open_or_right")}
            return {**detail, **sale("scheduled", day, op, cap)}
        t = stop_or_hold(q, c, stop, day, n > 1)
        if isinstance(t, dict):
            return {**detail, **t}
        if t:
            return {**detail, **sale(t[0], day, t[1], cap)}
    raise AssertionError()


def extension_reprice(q, r, use_prior):
    d, c = r["date"], r["code"]
    i = q.idx[d]
    buy, stop = D(r["buy"]), D(r["stop"])
    sell_day = q.days[i + 7]
    close, ma = q.close_ma(i + 6, c)
    (op, _, _, _), clean = q.session(sell_day, c)
    assert sell_day == r["exit_day"] and clean and op == D(r["exit_price"])
    if close is None or ma is None:
        return dict(extended=None, **unknown("previous_ma_missing"))
    extend = (close if use_prior else op) > buy and close > ma
    if not extend:
        return dict(extended=False, **old_outcome(r))
    sell_next = False
    for n in range(7, 21):
        day = q.days[i + n]
        (op, _, _, _), ok = q.session(day, c)
        if not ok or op is None:
            return dict(extended=True, **unknown(day + "/right_or_open_missing"))
        if n == 20 or sell_next:
            return dict(extended=True, **sale("ma5_exit" if sell_next else "max20", day, op, buy))
        st = stop_or_hold(q, c, stop, day, True)
        if isinstance(st, dict):
            return dict(extended=True, **st)
        if st:
            return dict(extended=True, **sale(st[0], day, st[1], buy))
        close, ma = q.close_ma(i + n, c)
        if close is None or ma is None:
            return dict(extended=True, **unknown(day + "/ma_missing"))
        sell_next = close < ma
    raise AssertionError()


def mark_path(q, decision, legs, daily_rows):
    initial = D(2_000_000)
    cash = initial - sum(
        (e["buy"] * SHARES * (1 + SLIP) + FEE) for _, e in legs if e["buy"] is not None
    )
    peak = initial
    worst = D(0)
    i = q.idx[decision]
    end_marks = max((e["final"] for _, e in legs if e["final"]), default=q.days[i + 1])
    assert daily_rows and daily_rows[-1]["session"] >= end_marks
    for n, r in enumerate(daily_rows, 1):
        day = r["session"]
        assert q.days[i + n] == day
        lot_value = D(0)
        count = 0
        for code, e in legs:
            if e["buy"] is None:
                continue
            if e["final"] == day:
                cash += e["sell"] * SHARES * (1 - SLIP) - FEE
            elif day < e["final"]:
                (_, _, _, close), _ = q.session(day, code)
                assert close is not None
                lot_value += close * SHARES
                count += 1
        nav = cash + lot_value
        peak = max(nav, peak)
        dd = (1 - nav / peak) * 100
        worst = max(worst, dd)
        assert abs(D(r["nav"]) - nav) < SUM_TOLERANCE
        assert abs(D(r["dd_pct"]) - dd) < SUM_TOLERANCE
        assert abs(D(r["cash"]) - cash) < SUM_TOLERANCE and int(r["positions"]) == count
    return worst


def verify_artifacts(folder, q, dates, output_trades, day_rows):
    all_orders = csv_rows(folder / "orders.csv")
    all_days = csv_rows(folder / "windows.csv")
    nav_by_date = defaultdict(list)
    for row in csv_rows(folder / "nav.csv"):
        nav_by_date[row["rule"], row["date"]].append(row)
    rule_keys = sorted(set(r["rule"] for r in all_orders))
    print("available study rule keys:", sorted(rule_keys))
    name_map = {name: name for name in rule_keys}
    checks = {}
    for rule, name in name_map.items():
        if name not in output_trades:
            continue
        rows = [r for r in all_orders if r["rule"] == rule and r["date"] in dates]
        ref = {(r["date"], r["code"]): r for r in rows}
        assert len(ref) == len(output_trades[name])
        n_cash = n_price = n_when = n_net = n_marks = 0
        legs_by_day = defaultdict(list)
        for key, e in output_trades[name].items():
            row = ref[key]
            legs_by_day[key[0]].append((key[1], e))
            expected = e["net"]
            got = (
                D(row["net"] or 0)
                if row["exit_type"] != "unresolved" and not row.get("issue")
                else None
            )
            if expected is not None:
                assert got is not None and abs(expected - got) < SUM_TOLERANCE, (name, key, e, row)
                n_net += 1
            else:
                assert got is None, (name, key, e, row)
            if e["sell"] is not None:
                assert value(row["exit_price"]) == e["sell"] and row["exit_day"] == e["final"], (
                    name,
                    key,
                    e,
                    row,
                )
                n_price += 1
                n_when += 1
            if e["buy"] is not None:
                assert value(row["buy"]) == e["buy"]
                n_cash += 1
            else:
                assert value(row["buy"]) is None
        for row in all_days:
            if row["rule"] != rule or row["date"] not in dates:
                continue
            truth = day_rows[name][row["date"]]
            if truth is None:
                assert row["status"] != "ok"
            else:
                marks = nav_by_date[rule, row["date"]]
                worst = mark_path(q, row["date"], legs_by_day[row["date"]], marks)
                assert abs(D(row["dd_pct"]) - worst) < SUM_TOLERANCE
                n_marks += len(marks)
                assert row["status"] == "ok" and abs(truth - D(row["net"])) < SUM_TOLERANCE, (
                    rule,
                    row,
                    truth,
                )
        checks[name] = dict(
            attempted_legs=len(ref),
            complete_leg_nets=n_net,
            simulated_buy_matches=n_cash,
            completed_sell_prices=n_price,
            completed_sell_dates=n_when,
            compared_dates=len(dates),
            complete_account_daily_marks=n_marks,
        )
    assert "limit_gu3" in checks
    return checks


def run(args):
    project = args.project.resolve()
    episodes = args.episodes
    pairs = csv_rows(episodes / "paired.csv")
    fixed = {
        r["date"]: D(r["guarded_net"])
        for r in pairs
        if all(r[k + "_status"] == "ok" for k in ("current", "plus3", "guarded"))
    }
    assert len(fixed) == 709
    orders = [
        r
        for r in csv_rows(episodes / "orders.csv")
        if r["rule"] == "guarded" and r["date"] in fixed
    ]
    assert len(orders) == 3532
    q = Quotes(project, orders)
    rules = ("limit_gu3", "strict_limit", "extend_ma5", "extend_prior")
    results = {v: {} for v in rules}
    daily = {v: defaultdict(list) for v in rules}
    events = []
    old_totals = Counter()
    for r in orders:
        key = r["date"], r["code"]
        previous = old_outcome(r)
        old_totals[r["date"]] += previous["net"]
        op_net = previous["net"]
        buy = value(r["buy"])
        if r["exit_type"] == "skip_gap":
            lim = limit_reprice(q, r)
            strict = skip("strict_touch_unfilled") if lim["touch_only"] else lim
            outcomes = [
                (lim, lim["price"]),
                (strict, None if strict is not lim else lim["price"]),
                (previous, buy),
                (previous, buy),
            ]
            events.append(
                dict(scope="limit", date=r["date"], code=r["code"], old_net=op_net, **lim)
            )
        elif r["exit_type"] == "scheduled":
            e, ep = (extension_reprice(q, r, prior) for prior in (False, True))
            outcomes = [(previous, buy), (previous, buy), (e, buy), (ep, buy)]
            for name, ext in [("extend_open", e), ("extend_preclose", ep)]:
                events.append(
                    dict(scope=name, date=r["date"], code=r["code"], old_net=op_net, **ext)
                )
        else:
            outcomes = [(previous, buy)] * 4
        for name, (record, entry_price) in zip(rules, outcomes):
            results[name][key] = dict(**record, buy=entry_price)
            daily[name][r["date"]].append(record["net"])
    assert all(abs(old_totals[d] - fixed[d]) < SUM_TOLERANCE for d in fixed)
    summed = {
        name: {d: sum(x, D(0)) if all(v is not None for v in x) else None for d, x in arr.items()}
        for name, arr in daily.items()
    }
    changes = [
        dict(date=d, guarded=fixed[d], **{k: summed[k][d] for k in rules}) for d in sorted(fixed)
    ]
    agg = {}
    for rule, var in summed.items():
        diffs = [v - fixed[d] for d, v in var.items() if v is not None]
        agg[rule] = dict(
            complete=len(diffs),
            total_net=sum(v for v in var.values() if v is not None),
            total_difference=sum(diffs),
            mean_difference=sum(diffs) / len(diffs),
            better=sum(v > SUM_TOLERANCE for v in diffs),
            worse=sum(v < -SUM_TOLERANCE for v in diffs),
            equal=sum(abs(v) <= SUM_TOLERANCE for v in diffs),
            unresolved=[d for d, x in var.items() if x is None],
        )
    summaries = {"comparison": agg}
    for scope in ("limit", "extend_open", "extend_preclose"):
        group = [e for e in events if e["scope"] == scope]
        selected = [e for e in group if e.get("extended", True)]
        summaries[scope] = dict(
            total_inputs=len(group),
            selected=len(selected),
            outcomes=dict(Counter(e["result"] for e in selected)),
            exact_limit_touches=sum(e.get("touch_only", False) for e in group),
            missing=[(e["date"], e["code"], e.get("issue")) for e in group if e["net"] is None],
        )
    if args.compare:
        summaries["checks"] = verify_artifacts(args.compare, q, set(fixed), results, summed)
    args.output.mkdir(parents=True, exist_ok=True)
    put_csv(args.output / "counterfactual_legs.csv", events)
    put_csv(args.output / "counterfactual_windows.csv", changes)
    (args.output / "checks.json").write_text(
        json.dumps(summaries, ensure_ascii=False, default=str, indent=2), encoding="utf-8"
    )
    print(json.dumps(summaries, ensure_ascii=False, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--episodes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    run(parser.parse_args())
