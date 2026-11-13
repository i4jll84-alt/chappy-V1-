"""Replay the fixed 10:00/13:30 long-only research rule on received Rakuten data."""

from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal as D

JST = timezone(timedelta(hours=9))
CAPITAL, LOT, FEE, SLIP = D("1000000"), 100, D("10"), D("0.0005")
SESSIONS = (("09:00", "10:00", "10:30"), ("12:30", "13:30", "14:00"))


def stamp(day, clock):
    return datetime.fromisoformat(day + "T" + clock).replace(tzinfo=JST).astimezone(UTC)


def fmt(t):
    return t.astimezone(UTC).isoformat(timespec="microseconds")


def read_signals(db, codes, day, session):
    start, decision, target = [stamp(day, t) for t in session]
    # Pick the last received version BEFORE inspecting quality, including revisions to NULL.
    sql = """
        WITH versions AS (
          SELECT *,ROW_NUMBER() OVER(PARTITION BY symbol,bar_at
                     ORDER BY known_at DESC,source_id DESC) AS revision
          FROM minute_bars WHERE source='rakuten' AND known_at<=?
            AND (bar_at=? OR (bar_at>=? AND bar_at<?))
        )
        SELECT * FROM versions WHERE revision=1 AND elapsed_minute=1
          AND volume>0 AND o>0 AND c>0 ORDER BY bar_at
    """
    latest, opening = {}, {}
    for b in db.execute(
        sql, (fmt(decision), fmt(start), fmt(decision - timedelta(minutes=5)), fmt(decision))
    ):
        assert (
            datetime.fromisoformat(b["bar_at"]) + timedelta(minutes=1)
            <= datetime.fromisoformat(b["known_at"])
            <= decision
        )
        if b["bar_at"] == fmt(start):
            opening[b["symbol"]] = b
        else:
            latest[b["symbol"]] = b
    signals = []
    for code in codes:
        o, c = opening.get(code), latest.get(code)
        signals.append(
            dict(
                day=day,
                symbol=code,
                decision_at=fmt(decision),
                exit_target=fmt(target),
                opening=o["o"] if o else None,
                opening_received=o["known_at"] if o else None,
                recent_close=c["c"] if c else None,
                recent_bar=c["bar_at"] if c else None,
                recent_received=c["known_at"] if c else None,
            )
        )
    return signals


def quotes(db, day):
    rows = db.execute(
        """SELECT * FROM quotes WHERE source='rakuten' AND source_quality='observed_book'
        AND known_at>=? AND known_at<? ORDER BY known_at,symbol,source_id""",
        (fmt(stamp(day, "09:00")), fmt(stamp(day, "15:25"))),
    )
    keep = []
    for q in rows:
        at = datetime.fromisoformat(q["known_at"])
        local = at.astimezone(JST).strftime("%H:%M")
        if "11:30" <= local < "12:30":
            continue
        start = stamp(day, "09:00" if local < "11:30" else "12:30")
        if datetime.fromisoformat(q["read_started_at"]) < start:
            continue
        if min(datetime.fromisoformat(q[k]) for k in ("bid_at", "ask_at")) < start:
            continue
        assert 0 < q["bid"] <= q["ask"] and min(q["bid_qty"], q["ask_qty"]) > 0
        assert all(
            0 <= (at - datetime.fromisoformat(q[k])).total_seconds() <= 60
            for k in ("bid_at", "ask_at")
        )
        keep.append(q)
    return keep


def money(px):
    gross = D(str(px)) * LOT
    return gross, gross * SLIP + FEE


def replay(dataset):
    arm = "rising"
    available = CAPITAL
    to_settle, positions, marks = Counter(), {}, {}
    decisions, fills, trades, days = [], [], [], []
    expected_count = 0
    for d, (day, daily_signals, books) in enumerate(dataset):
        expected_count += sum(len(s) for s in daily_signals)
        available += to_settle.pop(d, 0)
        bought_today, pending = set(), {}
        windows = [(stamp(day, s[1]), sig) for s, sig in zip(SESSIONS, daily_signals, strict=True)]
        events = [(t, 0, sig) for t, sig in windows]
        events.extend((datetime.fromisoformat(q["known_at"]), 1, q) for q in books)
        for now, kind, item in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == 0:
                pending.clear()
                for sig in item:
                    code = sig["symbol"]
                    row = dict(arm=arm, **sig, outcome="")
                    decisions.append(row)
                    if sig["opening"] is None or sig["recent_close"] is None:
                        row["outcome"] = "missing_input"
                    elif sig["recent_close"] <= sig["opening"]:
                        row["outcome"] = "not_rising"
                    elif code in positions or code in bought_today:
                        row["outcome"] = "position_or_already_bought_today"
                    else:
                        row["outcome"] = "no_qualifying_entry_quote"
                        pending[code] = row
                continue
            q, code = item, item["symbol"]
            marks[code] = (q["bid"], q["known_at"])
            pos = positions.get(code)
            signal = pending.get(code)
            if pos:
                decision, side = pos["exit_target"], "sell"
            elif signal:
                decision, side = signal["decision_at"], "buy"
                if now > datetime.fromisoformat(decision) + timedelta(minutes=5):
                    del pending[code]
                    continue
            else:
                continue
            column = "ask" if side == "buy" else "bid"
            if (
                q["read_started_at"] < decision
                or q[column + "_at"] <= decision
                or q[column + "_qty"] < LOT
            ):
                continue
            px = q[column]
            gross, costs = money(px)
            if side == "buy":
                del pending[code]
                if available < gross + costs:
                    signal["outcome"] = "insufficient_settled_cash"
                    continue
                available -= gross + costs
                signal["outcome"] = "bought"
                positions[code] = dict(
                    cost=gross + costs,
                    gross=gross,
                    at=q["known_at"],
                    exit_target=signal["exit_target"],
                )
                bought_today.add(code)
            else:
                to_settle[d + 2] += gross - costs
                del positions[code]
                trades.append(
                    dict(
                        arm=arm,
                        symbol=code,
                        bought_at=pos["at"],
                        sold_at=q["known_at"],
                        entry_notional=pos["gross"],
                        exit_notional=gross,
                        gross=gross - pos["gross"],
                        net=gross - costs - pos["cost"],
                        exit_target=decision,
                    )
                )
            fills.append(
                dict(
                    arm=arm,
                    symbol=code,
                    side=side,
                    lot=LOT,
                    source_id=q["source_id"],
                    decision_at=decision,
                    read_started_at=q["read_started_at"],
                    received_at=q["known_at"],
                    book_updated_at=q[column + "_at"],
                    price=px,
                    quantity_shown=q[column + "_qty"],
                    fee=FEE,
                    slippage=gross * SLIP,
                )
            )
        marked = sum((D(str(marks[c][0])) * LOT for c in positions), D("0"))
        equity = available + sum(to_settle.values()) + marked
        daily_trades = [t for t in trades if t["sold_at"][:10] == day]
        days.append(
            dict(
                day=day,
                arm=arm,
                round_trips=len(daily_trades),
                realized=sum(t["net"] for t in daily_trades),
                buys=sum(f["side"] == "buy" and f["received_at"][:10] == day for f in fills),
                settled_cash=available,
                unsettled=sum(to_settle.values()),
                open_positions=len(positions),
                marked_equity=equity,
                open_marks={c: marks[c] for c in positions},
            )
        )
    gross = sum(t["gross"] for t in trades)
    net = sum(t["net"] for t in trades)
    assert len(fills) == len(trades) * 2 + len(positions)
    assert days[-1]["marked_equity"] - CAPITAL == net + sum(
        D(str(marks[c][0])) * LOT - p["cost"] for c, p in positions.items()
    )
    assert len(decisions) == expected_count
    summary = dict(
        round_trips=len(trades),
        wins=sum(t["net"] > 0 for t in trades),
        losses=sum(t["net"] < 0 for t in trades),
        breakeven=sum(t["net"] == 0 for t in trades),
        symbols_traded=len({f["symbol"] for f in fills}),
        gross_closed=gross,
        net_closed=net,
        fees_all_fills=sum(f["fee"] for f in fills),
        slippage_all_fills=sum(f["slippage"] for f in fills),
        final_equity=days[-1]["marked_equity"],
        total_return_pct=(days[-1]["marked_equity"] / CAPITAL - 1) * 100,
        final_open_positions=len(positions),
        decisions=Counter(row["outcome"] for row in decisions),
    )
    return summary, decisions, fills, trades, days
