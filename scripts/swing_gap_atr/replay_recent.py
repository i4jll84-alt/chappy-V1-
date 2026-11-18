"""Replay 2026-11-16..18 forward-paper snapshots and the fixed gap/ATR counterfactual."""

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal as D
from functools import lru_cache
from pathlib import Path

import pyarrow.parquet as pq

DATES = ("2026-11-16", "2026-11-17", "2026-11-18")
FIELDS = ["Date", "Code", "O", "H", "L", "C", "AdjFactor", "ExRT"]
STRATEGIES = ("current", "plus3", "plus3_gap_atr")


def read_json(path):
    return json.loads(path.read_bytes())


@lru_cache(maxsize=256)
def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_path(root, path, expected):
    path = (root / path).resolve()
    if not path.is_relative_to(root) or digest(path) != expected:
        raise ValueError("Source hash or path mismatch: " + str(path))
    return path


def timestamp(value):
    out = datetime.fromisoformat(value)
    if out.tzinfo is None:
        raise ValueError("Source time has no timezone: " + value)
    return out


def daily_quotes(root, ref, asof, codes=None):
    assert timestamp(ref["completed_at"]) <= asof
    manifest_path = check_path(root, ref["manifest"], ref["sha256"])
    manifest = read_json(manifest_path)
    assert manifest["state"] == "verified"
    result = {}
    for page in manifest["pages"]:
        assert timestamp(page["available_at"]) <= asof
        path = check_path(manifest_path.parent, page["file"], page["sha256"])
        for row in read_json(path)["data"]:
            if codes is not None and row["Code"] not in codes:
                continue
            assert row["Date"] == ref["date"] and row["Code"] not in result
            result[row["Code"]] = row
    return result


def historical_window(jq, signal, codes):
    dates = signal["window"][-15:]
    assert (
        len(dates) == len(set(dates)) == 15
        and dates == sorted(dates)
        and dates[-1] == signal["date"]
    )
    asof = timestamp(signal["asof"])
    by_day = {d: {} for d in dates}
    daily_dates = set()
    for ref in signal["input_refs"]:
        if ref.get("date") in by_day and "manifest" in ref:
            by_day[ref["date"]] = daily_quotes(jq, ref, asof, codes)
            daily_dates.add(ref["date"])
    missing = set(dates) - daily_dates
    archive = jq / "history"
    sources = []
    with sqlite3.connect(
        (archive / "history.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True
    ) as db:
        for ref in signal["input_refs"]:
            if "path" not in ref or ref.get("endpoint") != "/equities/bars/daily":
                continue
            assert timestamp(ref["available_at"]) <= asof
            raw, rawhash, low, high = db.execute(
                "select raw_path,sha256,min_date,max_date from bulk_files "
                "where state='complete' and parquet_path=? and parquet_sha256=?",
                (ref["path"], ref["sha256"]),
            ).fetchone()
            required = sorted(d for d in missing if low <= d <= high)
            if not required:
                continue
            assert rawhash == ref["raw_sha256"]
            check_path(archive, raw, ref["raw_sha256"])
            path = check_path(archive, ref["path"], ref["sha256"])
            sources.append(ref["path"])
            table = pq.read_table(
                path,
                columns=FIELDS,
                filters=[("Date", "in", required), ("Code", "in", sorted(codes))],
            )
            for row in table.to_pylist():
                slots = by_day[row["Date"]]
                assert row["Code"] not in slots
                slots[row["Code"]] = row
    if any(not by_day[d] for d in dates):
        raise ValueError("Missing session in ATR inputs")
    return [by_day[d] for d in dates], dict(
        first=dates[0],
        last=dates[-1],
        sessions=len(dates),
        verified_snapshot_days=len(daily_dates),
        verified_archive_days=len(missing),
        archive_files=sources,
    )


def number(x):
    return D(str(x)) if x is not None and x != "" else None


def signal_atr(history, code):
    items = [d.get(code) for d in history]
    if any(r is None for r in items):
        return None
    ranges = []
    for prev, curr in zip(items, items[1:]):
        high, low, prior_close = [number(v) for v in (curr["H"], curr["L"], prev["C"])]
        if any(x is None or x <= 0 for x in (high, low, prior_close)) or high < low:
            return None
        if number(curr["AdjFactor"]) != 1 or curr["ExRT"] not in (None, ""):
            return None
        ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
    return sum(ranges) / len(ranges)


def bounded_settlement(sf, state, orders, eligible, quotes, history, day, due, stops):
    lookup = {r["code"]: r for r in eligible}
    filtered = {"buy": [], "sell": orders["sell"]}
    skipped, intended = {}, {}
    for c in orders["buy"]:
        bar = quotes.get(c, {})
        opening = number(bar.get("O"))
        prior_close = D(lookup[c]["close"])
        if opening is None or opening <= 0:
            skipped[c] = "missing_open"
            continue
        if number(bar.get("AdjFactor")) != 1 or bar.get("ExRT") not in (None, ""):
            skipped[c] = "entry_right"
            continue
        if opening > prior_close * D("1.03"):
            skipped[c] = "gap_above_3pct"
            continue
        atr = signal_atr(history, c)
        if atr is None:
            skipped[c] = "missing_atr"
            continue
        intended[c] = dict(atr=str(atr), stop=str(opening - 2 * atr), prior_close=str(prior_close))
        filtered["buy"].append(c)
    new, fills, metrics = sf.settle_account(state, filtered, quotes, day, due)
    assert new["status"] == "ok" and metrics is not None
    for f in fills:
        if f["status"] == "simulated" and f["action"] == "buy":
            stops[f["code"]] = D(intended[f["code"]]["stop"])
            f.update(intended[f["code"]])
        elif f["status"] == "simulated":
            del stops[f["code"]]
    exits = {}
    adjusted = dict(quotes)
    for c in new["positions"]:
        trigger = stops[c]
        opening = number(quotes[c]["O"])
        low = number(quotes[c]["L"])
        if opening is None or low is None or opening <= 0 or low <= 0:
            raise ValueError(f"Unobservable stop on {day} {c}")
        if opening <= trigger:
            exits[c] = "open_below_stop"
            adjusted[c] = dict(quotes[c], O=str(opening))
        elif low <= trigger:
            exits[c] = "intraday_stop"
            adjusted[c] = dict(quotes[c], O=str(trigger))
    if exits:
        # The official accounting helper marks at the close of each call.
        # Restore yesterday's high-water mark before computing the final daily close.
        for key in ("high", "max_dd"):
            new[key] = state[key]
        new, stop_fills, metrics = sf.settle_account(
            new, {"buy": [], "sell": list(exits)}, adjusted, day, due
        )
        assert metrics is not None
        for f in stop_fills:
            assert f["status"] == "simulated"
            f["reason"] = exits[f["code"]]
            f["trigger"] = str(stops.pop(f["code"]))
            fills.append(f)
    return new, fills, metrics, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="New JSON output file")
    args = parser.parse_args()
    if not __debug__:
        parser.error("The frozen-record checks require running Python without -O.")
    project = args.project.resolve()
    jq = project / "data/jquants"
    root = project / "data/swing-forward/sessions"
    sys.path.insert(0, str(project / "src"))
    from trading_system.research import swing_forward as sf

    states = {arm: sf.initial()["machine_current"] for arm in STRATEGIES}
    stops = {}
    daily = []
    totals = {s: Counter() for s in STRATEGIES}
    for day in DATES:
        folder = root / day
        pre = read_json(folder / "preopen.json")
        close = read_json(folder / "close.json")
        signal = pre["signal"]
        assert pre["target"] == close["target"] == day
        assert timestamp(pre["recorded_at"]).isoformat()[:16] < day + "T09:00"
        assert signal["date"] < day and not pre["stopped"] and "issue" not in signal
        assert (
            timestamp(signal["asof"])
            < timestamp(pre["recorded_at"])
            < timestamp(close["recorded_at"])
        )
        (bar_ref,) = [
            ref for ref in close["sources"] if ref.get("date") == day and "manifest" in ref
        ]
        bars = daily_quotes(jq, bar_ref, timestamp(close["recorded_at"]))
        pool = signal["candidates"]
        plus = [r for r in pool if not r["plus3_exclusions"]] if signal["credit_complete"] else []
        needed = {r["code"] for r in plus[:5]}
        history, sources = historical_window(jq, signal, needed)
        for c in needed:
            assert number(history[-1][c]["C"]) == D(
                next(r["close"] for r in plus if r["code"] == c)
            )
        record = dict(
            date=day,
            signal_date=signal["date"],
            cutoff=signal["asof"],
            preopen_sha256=digest(folder / "preopen.json"),
            close_sha256=digest(folder / "close.json"),
            credit_complete=signal["credit_complete"],
            atr_sources=sources,
            accounts={},
        )
        for arm in STRATEGIES:
            baseline = arm != "plus3_gap_atr"
            eligible = pool if arm == "current" else plus
            orders = sf.account_orders(states[arm], eligible, day)
            if baseline:
                key = "machine_" + arm
                assert states[arm] == pre["accounts_before"][key] and orders == pre["machine"][key]
                state, fills, metrics = sf.settle_account(
                    states[arm], orders, bars, day, pre["exit_if_bought"]
                )
                assert (state, fills, metrics) == (
                    close["accounts"][key],
                    close["fills"][key],
                    close["metrics"][key],
                ), (day, arm)
                skipped = {}
            else:
                state, fills, metrics, skipped = bounded_settlement(
                    sf,
                    states[arm],
                    orders,
                    eligible,
                    bars,
                    history,
                    day,
                    pre["exit_if_bought"],
                    stops,
                )
            states[arm] = state
            for f in fills:
                if f["status"] == "simulated":
                    totals[arm][f["action"]] += 1
                    if f.get("reason") in ("open_below_stop", "intraday_stop"):
                        totals[arm]["stops"] += 1
            totals[arm].update(skipped.values())
            record["accounts"][arm] = dict(
                orders=orders,
                skips=skipped,
                fills=fills,
                metrics=metrics,
                positions=state["positions"],
                cash=state["cash"],
            )
        daily.append(record)
    summary = {
        arm: dict(
            metrics=daily[-1]["accounts"][arm]["metrics"],
            totals=dict(totals[arm]),
            positions=len(states[arm]["positions"]),
        )
        for arm in STRATEGIES
    }
    report = dict(
        start=DATES[0],
        end=DATES[-1],
        initial_capital=2000000,
        baseline_account_days_matched=6,
        summary=summary,
        daily=daily,
    )
    with args.out.open("x", encoding="utf8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
