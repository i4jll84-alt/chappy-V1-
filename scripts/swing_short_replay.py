"""Replay the registered short hypotheses on the seven existing macro sessions."""

import hashlib
import json
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal as D
from pathlib import Path

from trading_system.research import swing_forward as sf
from trading_system.research import swing_shorts as short
from trading_system.research.daytrade_universe import dated_inputs
from trading_system.research.swing_short_sources import JSF, PublicInputs

ASOF = datetime.fromisoformat("2026-11-13T18:00:00+09:00")
THROUGH = "2026-11-13"
ROOT = Path.cwd()
OUT = ROOT / "data/offline-verification/swing-short-20261113"


def reverse_fee_table():
    p = PublicInputs(ROOT, ASOF)
    values = {}
    try:
        for table in p.files(JSF + "shina.csv", 30):
            header = next(i for i, r in enumerate(table) if r[:2] == ["貸借申込日", "決済日"])
            for r in table[header + 1 :]:
                if len(r) < 12 or r[4] != "東証":
                    continue
                try:
                    day = date.fromisoformat(r[0]).isoformat()
                except ValueError:
                    continue
                v = r[10].strip()
                if v and (v.strip("*＊") == "" or v.replace(".", "", 1).isdigit()):
                    values.setdefault((day, r[2] + "0"), D(0) if v.strip("*＊") == "" else D(v))
        return values, p.refs
    finally:
        p.db.close()


def make_packets():
    base = ROOT / "data/offline-verification/swing-market-replay-20261113"
    body = (base / "inputs-v2.json").read_bytes()
    if hashlib.sha256(body).hexdigest() != sf.read(base / "plan-v2.json")["inputs_sha256"]:
        raise ValueError("macro_input_mismatch")
    cases = json.loads(body)
    calendar = sf.calendar(ROOT / "data/jquants", date.fromisoformat(THROUGH), ASOF)
    packets = {}
    for case in sorted(cases.values(), key=lambda x: x["asof"]):
        cutoff = datetime.fromisoformat(case["asof"])
        day = str(cutoff.date())
        i = calendar.index(day)
        prev = calendar[i - 1]
        selection_cut = datetime.combine(date.fromisoformat(prev), time(23, 59, 59), sf.JST)
        data = sf.screen(ROOT, date.fromisoformat(prev), selection_cut)
        versions = {}
        for name, at in (("07:55", cutoff), ("08:15", cutoff.replace(hour=8, minute=15))):
            packet = short.prepare(
                ROOT,
                day,
                data["window"],
                data["short_charts"],
                {"series": case["market"]},
                at,
                reconstructed=True,
            )
            versions[name] = packet
        packets[day] = versions
    return calendar, packets


def run_book(arm, clock, calendar, packets, prices, reverse, matched=False):
    positions = {}
    closed = []
    marks = []
    skipped = Counter()
    for day, quotes in prices.items():
        i = calendar.index(day)
        for code, lot in list(positions.items()):
            bar = quotes.get(code)
            if not bar or D(str(bar["AdjFactor"])) != 1 or bar["ExRT"] not in (None, ""):
                raise ValueError(f"unresolved holding action {day} {code}")
            if day >= lot["due"]:
                if not bar["O"] or D(str(bar["O"])) <= 0:
                    skipped["missing_cover_open"] += 1
                    continue
                px = D(str(bar["O"]))
                closed.append(value(lot, px, day, calendar, reverse, True))
                del positions[code]
        packet = packets.get(day, {}).get(clock)
        codes = short.choices(packet, arm) if packet else []
        if matched and packet:
            common = {
                r["code"]
                for r in packet["candidates"]
                if packet["market_state"] != "market_data_missing"
                and not any("missing" in flag for flag in r["credit"]["exclusions"])
            }
            codes = [c for c in codes if c in common]
        for code in codes:
            bar = quotes.get(code, {})
            if code in positions:
                skipped["already_held"] += 1
                continue
            if (
                not bar.get("O")
                or D(str(bar["O"])) <= 0
                or D(str(bar["AdjFactor"])) != 1
                or bar["ExRT"] not in (None, "")
            ):
                skipped["missing_open_or_rights"] += 1
                continue
            px = D(str(bar["O"]))
            # Reserve the full sale notional; do not spend short sale proceeds.
            spent = sum(pos["entry_price"] * 100 for pos in positions.values())
            equity = D(2000000) + sum(x["before_reverse"] - x["known_reverse"] for x in closed)
            if positions:
                if any(not quotes[c].get("O") for c in positions):
                    raise ValueError("held_open_missing")
                for c, pos in positions.items():
                    mark = value(
                        pos,
                        D(str(quotes[c]["O"])),
                        day,
                        calendar,
                        reverse,
                        False,
                        include_day=False,
                    )
                    equity += mark["before_reverse"] - mark["known_reverse"]
            if spent + px * 100 + sf.FEE + sf.SLIP * px * 100 > equity:
                skipped["capital"] += 1
                continue
            positions[code] = dict(code=code, entry=day, entry_price=px, due=calendar[i + 6])
        open_marks = []
        for code, lot in positions.items():
            if not quotes[code]["C"] or D(str(quotes[code]["C"])) <= 0:
                raise ValueError("missing_mark")
            open_marks.append(value(lot, D(str(quotes[code]["C"])), day, calendar, reverse, False))
        lots = closed + open_marks
        marks.append(
            dict(
                day=day,
                closed=len(closed),
                open=len(positions),
                before_reverse=sum((x["before_reverse"] for x in lots), D(0)),
                known_reverse=sum((x["known_reverse"] for x in lots), D(0)),
                missing_reverse_stock_days=sum(x["missing_reverse_stock_days"] for x in lots),
                gross=sum((x["gross"] for x in lots), D(0)),
                notional=sum((x["entry_price"] * 100 for x in lots), D(0)),
            )
        )
    return dict(
        final=marks[-1],
        daily=marks,
        closed_trades=closed,
        open_marks=open_marks,
        skips=dict(skipped),
    )


def value(lot, px, day, calendar, reverse, closed, include_day=None):
    entry = lot["entry_price"]
    start = calendar.index(lot["entry"])
    end = calendar.index(day)
    days = (
        date.fromisoformat(calendar[end + 2]) - date.fromisoformat(calendar[start + 2])
    ).days + 1
    borrow = entry * 100 * D("0.011") * days / 365
    cost = sf.FEE + sf.SLIP * entry * 100
    if closed:
        cost += sf.FEE + sf.SLIP * px * 100
    gross = (entry - px) * 100
    # The closing mark still holds the position over today's close.
    carry = [
        reverse.get((d, lot["code"]))
        for d in calendar[start : end + int(not closed if include_day is None else include_day)]
    ]
    return dict(
        **lot,
        valuation_date=day,
        price=px,
        gross=gross,
        cost=cost,
        borrow=borrow,
        known_reverse=sum((v * 100 for v in carry if v is not None), D(0)),
        missing_reverse_stock_days=carry.count(None),
        before_reverse=gross - borrow - cost,
    )


def run():
    cal, packets = make_packets()
    start = min(packets)
    prices = {}
    for day in cal:
        if start <= day <= THROUGH:
            cols, _, _ = dated_inputs(ROOT / "data/jquants", [day], ASOF)
            prices[day] = {c: r[0] for c, r in cols.items()}
    reverse, refs = reverse_fee_table()
    books = {}
    for clock in ("07:55", "08:15"):
        books[clock] = {a: run_book(a, clock, cal, packets, prices, reverse) for a in short.ARMS}
    matched_books = {
        a: run_book(a, "08:15", cal, packets, prices, reverse, matched=True) for a in short.ARMS
    }
    OUT.mkdir(parents=True, exist_ok=True)
    sf.write(OUT / "signals.json", packets)
    sf.write(
        OUT / "result.json",
        json.loads(
            json.dumps(
                dict(
                    through=THROUGH,
                    books=books,
                    common_input_books=matched_books,
                    reverse_sources=refs,
                    confirmed_broker_fills=0,
                    inputs_sha256=sf.signature(packets),
                ),
                default=str,
            )
        ),
    )
    for clock, arms in books.items():
        print(
            clock,
            [
                (d, v[clock]["state"], *[len(short.choices(v[clock], a)) for a in short.ARMS])
                for d, v in packets.items()
            ],
        )
        print(clock, {k: v["final"] for k, v in arms.items()})


if __name__ == "__main__":
    run()
