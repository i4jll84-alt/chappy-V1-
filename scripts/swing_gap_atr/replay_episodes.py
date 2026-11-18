"""Replay archived six-session paper episodes with fixed candidate rankings."""

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import mean, median

import duckdb

D = Decimal
CAPITAL, SHARES = D(2_000_000), 100
SLIP, FEE = D("0.0005"), D(10)
RULES = ("current", "plus3", "guarded")
FIELDS = ("O", "H", "L", "C", "AdjFactor", "ExRT")
# Entry-date conversions require an announcement available before selection and a
# matching ex-rights flag/factor in the price archive. Other events stay unresolved.
CONFIRMED_SPLITS = {
    ("2025-07-30", "55270"): {
        "notice_date": "2025-07-15",
        "shares_after_per_share_before": 3,
        "source": "https://www2.jpx.co.jp/disc/55270/140120250714514214.pdf",
    },
}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def number(v):
    return D(str(v)) if v not in (None, "") else None


def positive(v):
    return v is not None and v > 0


def bar(values):
    o, h, low, c, factor, ex = values
    return tuple(map(number, (o, h, low, c))) + (number(factor) == 1 and not ex, number(factor), ex)


def load_bars(root, source_dir, picks, schedules):
    frozen = json.loads((source_dir / "sources.json").read_bytes())
    checked = {}

    def verify(path):
        path = path.resolve()
        key = str(path.relative_to(root))
        actual = sha(path)
        assert actual == frozen[key], key
        checked[key] = actual

    hist = root / "data/jquants/history"
    db_path = hist / "history.sqlite"
    wal = db_path.with_suffix(".sqlite-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    with sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        parts = db.execute(
            """SELECT endpoint, parquet_path FROM bulk_files
               WHERE endpoint IN ('/markets/calendar','/equities/bars/daily')
               AND state='complete' AND min_date<=? AND max_date>=?""",
            (max(schedules), "2023-10-10"),
        ).fetchall()
    calendars = [hist / rel for ep, rel in parts if ep == "/markets/calendar"]
    prices = [hist / rel for ep, rel in parts if ep == "/equities/bars/daily"]
    for path in calendars + prices:
        verify(path)
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        db.from_parquet([str(p) for p in calendars]).create_view("calendar")
        days = [
            str(v[0])
            for v in db.execute(
                "SELECT Date FROM calendar WHERE HolDiv IN ('1','2') ORDER BY Date"
            ).fetchall()
        ]
        indices = {d: i for i, d in enumerate(days)}
        needed = set()
        for (day, arm), codes in picks.items():
            i = indices[day]
            assert days[i + 1] == schedules[day]["entry"] and days[i + 7] == schedules[day]["exit"]
            for code in codes:
                needed.update((day2, code) for day2 in days[i - 14 : i + 8])
        # The archive spans a few post-October exits supplied by daily snapshots.
        db.from_parquet([str(p) for p in prices]).create_view("bars")
        import pyarrow as pa

        need_day, need_code = zip(*sorted(needed))
        db.register("needed", pa.table({"day": need_day, "code": need_code}))
        rows = db.execute(
            "SELECT b.Date,b.Code,b.O,b.H,b.L,b.C,b.AdjFactor,b.ExRT FROM bars b "
            "JOIN needed n ON b.Date=n.day AND b.Code=n.code WHERE b.Date<=?",
            [max(schedules)],
        ).fetchall()
    market = {(str(day), code): bar(vals) for day, code, *vals in rows}
    assert len(rows) == len(market)
    for day in sorted({d for d, _ in needed if d > max(schedules)}):
        paths = [
            root / key
            for key in frozen
            if "daily-snapshots" in key
            and day in Path(key).parts
            and Path(key).name.startswith("page-")
        ]
        assert paths, day
        for path in paths:
            verify(path)
            for q in json.loads(path.read_bytes())["data"]:
                key = q["Date"], q["Code"]
                assert key[0] == day and key not in market
                if key in needed:
                    market[key] = bar([q.get(f) for f in FIELDS])
    return days, indices, market, checked


EMPTY = (None, None, None, None, False, None, None)


def average_true_range(days, i, market, code):
    last_close = market.get((days[i - 14], code), EMPTY)[3]
    if not positive(last_close):
        return None
    total = D(0)
    for day in days[i - 13 : i + 1]:
        _, h, low, c, clean, _, _ = market.get((day, code), EMPTY)
        if not clean or not all(positive(v) for v in (h, low, c)) or not low <= c <= h:
            return None
        total += max(h - low, abs(h - last_close), abs(low - last_close))
        last_close = c
    return total / 14 if total > 0 else None


def simulate(day, rule, names, days, i, market, *, confirmed_splits=CONFIRMED_SPLITS):
    guarded = rule == "guarded"
    positions, actions = {}, []
    cash = peak = CAPITAL
    worst = D(0)
    counts = Counter()
    issues = []
    for j in range(i + 1, i + 8):
        session = days[j]
        if j == i + 1:
            for rank, code in enumerate(names, 1):
                row = dict(
                    date=day,
                    rule=rule,
                    rank=rank,
                    code=code,
                    entry_day=session,
                    buy=None,
                    entry_fee=None,
                    entry_slippage=None,
                    atr14=None,
                    stop=None,
                    exit_day=None,
                    exit_type=None,
                    exit_price=None,
                    exit_fee=None,
                    exit_slippage=None,
                    net=None,
                    issue=None,
                )
                actions.append(row)
                o, _, _, _, clean, factor, ex_flag = market.get((session, code), EMPTY)
                if not positive(o):
                    row["exit_type"] = "skip_open"
                elif guarded:
                    signal_close = market.get((day, code), EMPTY)[3]
                    atr = average_true_range(days, i, market, code)
                    event = confirmed_splits.get((session, code))
                    if event and not clean and event["notice_date"] <= day:
                        ratio = D(event["shares_after_per_share_before"])
                        if (
                            ex_flag == "1"
                            and positive(factor)
                            and abs(factor * ratio - 1) < D("1e-12")
                        ):
                            signal_close = signal_close / ratio if positive(signal_close) else None
                            atr = atr / ratio if atr is not None else None
                            clean = True
                    row["atr14"] = atr
                    if not clean or not positive(signal_close):
                        row["exit_type"] = "entry_indeterminate"
                        row["issue"] = session + "/incomparable_open_or_reference"
                        issues.append(code + "/" + row["issue"])
                    elif o > signal_close * D("1.03"):
                        row["exit_type"] = "skip_gap"
                    elif atr is None:
                        row["exit_type"] = "skip_atr"
                    else:
                        row["stop"] = o - 2 * atr
                if row["exit_type"] is None and cash < o * SHARES * (1 + SLIP) + FEE:
                    row["exit_type"] = "skip_cash"
                if row["exit_type"]:
                    counts[row["exit_type"]] += 1
                    continue
                gross = o * SHARES
                cash -= gross * (1 + SLIP) + FEE
                row.update(buy=o, entry_fee=FEE, entry_slippage=gross * SLIP)
                positions[code] = row
        nav = cash
        for code, row in list(positions.items()):
            if guarded and row["issue"]:
                continue
            o, h, low, c, clean, _, _ = market.get((session, code), EMPTY)
            reason = None
            if j > i + 1 and not clean:
                reason = "rights_or_missing_row"
            exit_type = None
            if j == i + 7:
                if positive(o):
                    exit_type, sell = "scheduled", o
                else:
                    reason = reason or "exit_open_missing"
            elif guarded and reason is None:
                if not positive(o):
                    reason = "held_open_missing"
                elif positive(row["stop"]) and o <= row["stop"]:
                    exit_type, sell = "stop_open", o
                elif not positive(low) or low > o or (positive(c) and low > c):
                    reason = "held_low_missing_or_invalid"
                elif positive(row["stop"]) and low <= row["stop"]:
                    exit_type, sell = "stop_touch", row["stop"]
            if not exit_type and not positive(c):
                reason = reason or "mark_missing"
            if reason:
                # Unresolved exposure persists; never use later data to make the exit determinate.
                if not row["issue"]:
                    row["issue"] = session + "/" + reason
                    issues.append(code + "/" + row["issue"])
                if guarded:
                    continue
            if exit_type:
                gross = sell * SHARES
                proceeds = gross * (1 - SLIP) - FEE
                cash += proceeds
                nav += proceeds
                row.update(
                    exit_day=session,
                    exit_type=exit_type,
                    exit_price=sell,
                    exit_fee=FEE,
                    exit_slippage=gross * SLIP,
                    net=proceeds - (row["buy"] * SHARES * (1 + SLIP) + FEE),
                )
                del positions[code]
                counts[exit_type] += 1
            elif positive(c):
                nav += c * SHARES
        peak = max(peak, nav)
        worst = max(worst, 1 - nav / peak)
    counts["buys"] = sum(a["buy"] is not None for a in actions)
    if not issues:
        assert not positions
        trade_net = sum((a["net"] for a in actions if a["net"] is not None), D(0))
        assert abs(trade_net - (cash - CAPITAL)) < D("0.00000000001")
    if guarded and issues:
        for a in actions:
            if a["issue"]:
                a.update(
                    exit_day=None,
                    exit_type="unresolved",
                    exit_price=None,
                    exit_fee=None,
                    exit_slippage=None,
                    net=None,
                )
    fees = sum((a["entry_fee"] or D(0)) + (a["exit_fee"] or D(0)) for a in actions)
    slippage = sum((a["entry_slippage"] or D(0)) + (a["exit_slippage"] or D(0)) for a in actions)
    rec = dict(
        date=day,
        rule=rule,
        status="unresolved" if issues else "ok",
        net=None if issues else cash - CAPITAL,
        dd_pct=None if issues else worst * 100,
        wins=None if issues else sum(a["net"] > 0 for a in actions if a["net"] is not None),
        candidates=len(names),
        buys=counts["buys"],
        skips=sum(v for k, v in counts.items() if k.startswith("skip_")),
        gap_skips=counts["skip_gap"],
        atr_skips=counts["skip_atr"],
        entry_indeterminate=counts["entry_indeterminate"],
        open_skips=counts["skip_open"],
        cash_skips=counts["skip_cash"],
        stop_touch=sum(a["exit_type"] == "stop_touch" for a in actions),
        stop_open=sum(a["exit_type"] == "stop_open" for a in actions),
        scheduled=sum(a["exit_type"] == "scheduled" for a in actions),
        fees=fees,
        slippage=slippage,
        issues=";".join(issues),
    )
    return rec, actions


def summarize(records, dates):
    common = [d for d in dates if all(records[d, r]["status"] == "ok" for r in RULES)]
    result = dict(planned=len(dates), common=len(common))
    for rule in RULES:
        rows = [records[d, rule] for d in common]
        vals = [r["net"] for r in rows]
        result[rule] = dict(
            own_complete=sum(records[d, rule]["status"] == "ok" for d in dates),
            positive=sum(v > 0 for v in vals),
            mean_net=mean(vals),
            median_net=median(vals),
            mean_return_pct=mean(vals) / CAPITAL * 100,
            min_net=min(vals),
            mean_episode_max_dd_pct=mean(r["dd_pct"] for r in rows),
            **{
                k: sum(r[k] for r in rows)
                for k in (
                    "buys",
                    "gap_skips",
                    "atr_skips",
                    "entry_indeterminate",
                    "stop_touch",
                    "stop_open",
                    "scheduled",
                    "fees",
                    "slippage",
                )
            },
        )
    delta = [records[d, "guarded"]["net"] - records[d, "plus3"]["net"] for d in common]
    result["guarded_minus_plus3"] = dict(
        mean=mean(delta),
        median=median(delta),
        better=sum(x > 0 for x in delta),
        tied=delta.count(0),
        worse=sum(x < 0 for x in delta),
    )
    return result


def run(root, out):
    root, out = root.resolve(), out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    origin = root / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs"
    schedules = {
        r["decision"]: r for r in read_csv(origin / "coverage.csv") if r["reference"] == "False"
    }
    dates = sorted(schedules)
    assert len(dates) == 730 and dates[0] == "2023-11-01" and dates[-1] == "2026-10-30"
    picks = defaultdict(list)
    for r in read_csv(origin / "selection.csv"):
        if r["decision"] in schedules:
            key = r["decision"], r["arm"]
            assert int(r["rank"]) == len(picks[key]) + 1 and r["code"] not in picks[key]
            picks[key].append(r["code"])
    days, indices, market, checked = load_bars(root, origin, picks, schedules)
    archive = {
        (r["decision"], r["arm"]): r
        for r in read_csv(origin / "windows.csv")
        if int(r["capital"]) == CAPITAL and r["decision"] in schedules
    }
    archive_trades = {
        (r["decision"], r["arm"], r["code"]): r
        for r in read_csv(origin / "trades.csv")
        if int(r["capital"]) == CAPITAL and r["decision"] in schedules
    }
    records, actions, checked_trades = {}, [], set()
    for day in dates:
        for rule in RULES:
            arm = "old" if rule == "current" else "new"
            rec, moves = simulate(day, rule, picks[day, arm], days, indices[day], market)
            if rule != "guarded":
                prev = archive[day, arm]
                assert rec["status"] == prev["status"] and rec["skips"] == int(prev["skips"]), (
                    day,
                    rule,
                    rec,
                    prev,
                )
                if rec["status"] == "ok":
                    assert rec["net"] == D(prev["net"]) and rec["dd_pct"] == D(prev["dd"]), (
                        day,
                        rule,
                        rec,
                        prev,
                    )
                    assert rec["buys"] == D(prev["executed"]) and rec["wins"] == D(prev["wins"])
                    for fill in moves:
                        if fill["net"] is None:
                            continue
                        key = day, arm, fill["code"]
                        stored = archive_trades[key]
                        assert (
                            fill["buy"] * SHARES,
                            fill["exit_price"] * SHARES,
                            fill["net"],
                        ) == tuple(D(stored[k]) for k in ("buy", "sell", "net"))
                        checked_trades.add(key)
            records[day, rule] = rec
            actions.extend(moves)
    assert checked_trades == set(archive_trades)
    report = dict(
        dates=[dates[0], dates[-1]],
        capital=CAPITAL,
        baseline_statuses_verified=len(archive),
        baseline_nets_and_drawdowns_verified=sum(r["status"] == "ok" for r in archive.values()),
        baseline_trade_details_verified=len(checked_trades),
        price_source_files_verified=len(checked),
        all=summarize(records, dates),
        by_year={
            y: summarize(records, [d for d in dates if d.startswith(y)])
            for y in sorted({d[:4] for d in dates})
        },
        offsets={str(k): summarize(records, dates[k::7]) for k in range(7)},
    )
    pairs = []
    for d in dates:
        row = {"date": d}
        for r in RULES:
            row[r + "_status"] = records[d, r]["status"]
            row[r + "_net"] = records[d, r]["net"]
        row["guarded_minus_plus3"] = (
            row["guarded_net"] - row["plus3_net"]
            if all(row[r + "_net"] is not None for r in ("plus3", "guarded"))
            else None
        )
        pairs.append(row)
    pair_dates = [
        d for d in dates if all(records[d, r]["status"] == "ok" for r in ("plus3", "guarded"))
    ]
    pair_delta = [records[d, "guarded"]["net"] - records[d, "plus3"]["net"] for d in pair_dates]
    report["plus3_and_guarded_complete"] = dict(
        n=len(pair_dates),
        mean_difference=mean(pair_delta),
        better=sum(x > 0 for x in pair_delta),
        worse=sum(x < 0 for x in pair_delta),
        tied=pair_delta.count(0),
    )
    affected_dates = [
        d
        for d in dates
        if any((schedules[d]["entry"], c) in CONFIRMED_SPLITS for c in picks[d, "new"])
    ]
    eligible = [d for d in dates if d not in affected_dates]
    report["entry_rights_or_missing_signal_close_unresolved_sensitivity"] = dict(
        excluded=[d for d in dates if d not in eligible],
        common_summary=summarize(records, eligible),
    )
    # The issuer announced the only affected entry split before the decision date.
    rights_date, rights_code = "2025-07-29", "55270"
    split_day = schedules[rights_date]["entry"]
    assert [d for d in dates if d not in eligible] == [rights_date]
    assert records[rights_date, "guarded"]["entry_indeterminate"] == 0
    rerun, split_actions = simulate(
        rights_date,
        "guarded",
        picks[rights_date, "new"],
        days,
        indices[rights_date],
        market,
        confirmed_splits={},
    )
    assert rerun["status"] == "unresolved" and rerun["entry_indeterminate"] == 1
    strict_actions = [a for a in actions if a["date"] == rights_date and a["rule"] == "guarded"]
    affected = next(a for a in strict_actions if a["code"] == rights_code)
    assert [a for a in strict_actions if a["code"] != rights_code] == [
        a for a in split_actions if a["code"] != rights_code
    ]
    report["verified_entry_split"] = dict(
        **CONFIRMED_SPLITS[split_day, rights_code],
        ex_date=split_day,
        previous_close=market[rights_date, rights_code][3],
        adjusted_previous_close=market[rights_date, rights_code][3] / 3,
        archived_factor=market[split_day, rights_code][5],
        archived_exrt=market[split_day, rights_code][6],
        entry_fill=affected,
        episode_net=records[rights_date, "guarded"]["net"],
        unverified_status=rerun["status"],
    )
    write_csv(out / "windows.csv", list(records.values()))
    write_csv(out / "orders.csv", actions)
    write_csv(out / "paired.csv", pairs)
    for name in ("selection.csv", "windows.csv", "trades.csv", "coverage.csv", "sources.json"):
        checked[str((origin / name).relative_to(root))] = sha(origin / name)
    (out / "verified_sources.json").write_text(
        json.dumps(checked, indent=2, sort_keys=True), encoding="utf8"
    )
    (out / "summary.json").write_text(json.dumps(report, default=str, indent=2), encoding="utf8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("by_year", "offsets")}, default=str
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    run(args.project, args.out)
