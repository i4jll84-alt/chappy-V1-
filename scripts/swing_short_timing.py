"""Replay separate credit and market filters on both previously frozen short-sale samples."""

import json
from collections import Counter, defaultdict
from statistics import mean

from swing_short_controls import fee_tables, pnl
from swing_short_extension import CLOCK, OUT, get_history, summary
from swing_short_replay import THROUGH, run_book

from trading_system.research import swing_forward as sf
from trading_system.research.swing_shorts import choices

DEST = OUT.parent / "swing-short-timing-20261113" / "result.json"


def compare(packets, calendar, prices, fees):
    passing = {d: p for d, p in packets.items() if p[CLOCK]["market_state"] == "pass"}
    return {
        "chart": run_book("chart", CLOCK, calendar, packets, prices, fees),
        "credit": run_book("credit", CLOCK, calendar, packets, prices, fees),
        "chart_market": run_book("chart", CLOCK, calendar, passing, prices, fees),
        "market": run_book("market", CLOCK, calendar, packets, prices, fees),
    }


def timing_days(mature, calendar, prices, fees):
    """Weight entry dates equally and evaluate each day's credit signals in isolation."""
    days = {}
    returns = defaultdict(list)
    for d, packet in mature.items():
        state = packet[CLOCK]["market_state"]
        credit = choices(packet[CLOCK], "credit")
        row = {"market_state": state, "credit_count": len(credit)}
        if credit:
            horizon = calendar[calendar.index(d) : calendar.index(d) + 7]
            book = run_book(
                "credit", CLOCK, calendar, {d: packet}, {t: prices[t] for t in horizon}, fees
            )
            lots = book["closed_trades"]
            if len(lots) != len(credit) or book["open_marks"] or book["skips"]:
                raise ValueError(f"incomplete same-date comparison on {d}")
            rate = mean(pnl(lot) / (lot["entry_price"] * 100) * 100 for lot in lots)
            row.update(
                mean_trade_pct=rate,
                missing_fee_days=sum(x["missing_reverse_stock_days"] for x in lots),
            )
            returns[state].append(rate)
        days[d] = row
    return {
        "dates": days,
        "groups": {
            state: {"days_with_signals": len(samples), "mean_daily_trade_pct": mean(samples)}
            for state, samples in returns.items()
        },
    }


def study(packets, first, calendar, quotes, fees, reference):
    dates = [d for d in calendar if first <= d <= THROUGH]
    packets = {d: packets[d] for d in dates}
    prices = {d: quotes[d] for d in dates}
    mature = {d: p for d, p in packets.items() if calendar[calendar.index(d) + 6] <= THROUGH}
    result = {}
    for window, inputs in [("all", packets), ("mature", mature)]:
        books = compare(inputs, calendar, prices, fees)
        if window == "all":
            for arm in ("chart", "market"):
                if json.loads(json.dumps(books[arm], default=str)) != reference[arm]:
                    raise ValueError(f"baseline no longer reproduces: {first} {arm}")
        elif any(b["open_marks"] for b in books.values()):
            raise ValueError("mature cohort has open positions")
        result[window] = {
            "first_entry": min(inputs),
            "last_entry": max(inputs),
            "market_days": dict(Counter(p[CLOCK]["market_state"] for p in inputs.values())),
            "arms": {name: summary(book) for name, book in books.items()},
        }
    result["independent_entry_days"] = timing_days(mature, calendar, prices, fees)
    return result


def main():
    calendar, quotes, _, _, _ = get_history()
    fees, historical = fee_tables(calendar)
    packets = sf.read(OUT / "signals.json")
    books = sf.read(OUT / "books.json")
    result = {
        "through": THROUGH,
        "signals_sha256": sf.signature(packets),
        "reference_books_sha256": sf.signature(books),
        "original": study(
            packets["original"],
            "2026-11-04",
            calendar,
            quotes,
            fees,
            books["original_prior_close_cut"],
        ),
        "us": study(
            packets["us"], "2026-10-07", calendar, quotes, historical, books["us_sensitivity"]
        ),
    }
    sf.write(DEST, json.loads(json.dumps(result, default=str)))
    print(DEST)


if __name__ == "__main__":
    main()
