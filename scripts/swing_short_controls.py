"""Compare short selections with all same-date, same-count chart alternatives on saved inputs."""

import itertools
import json
from collections import Counter
from decimal import Decimal as D
from statistics import median

from swing_short_extension import CLOCK, OUT, get_history, historical_jsf
from swing_short_replay import THROUGH, reverse_fee_table, run_book, value

from trading_system.research import swing_forward as sf

DEST = OUT.parent / "swing-short-controls-20261113"


def pnl(lot):
    return D(lot["before_reverse"]) - D(lot["known_reverse"])


def path_costs(lots):
    amounts = [pnl(r) for r in lots]
    return sum(amounts, D(0)), sum(
        (n / (D(r["entry_price"]) * 100) * 100 for n, r in zip(amounts, lots, strict=True)), D(0)
    ) / len(lots)


def evaluate(mode, packets, reference, calendar, prices, fees):
    target = reference["closed_trades"]
    if not target or reference["open_marks"]:
        raise ValueError("study needs completed selected trades")
    entries = [row["entry"] for row in target]
    if len(set(entries)) != len(entries):
        raise ValueError("one fixed 100-share entry per selected day required")
    if entries != sorted(entries):
        raise ValueError("unexpected entry order")
    actual = tuple(row["code"] for row in target)
    # Prices and outcomes are read only after all candidate sets have been established.
    candidates = {day: [r["code"] for r in packets[day][CLOCK]["candidates"]] for day in entries}
    if any(code not in candidates[day] for day, code in zip(entries, actual, strict=True)):
        raise ValueError("selected lot outside chart universe")
    days = {}
    reasons = Counter()
    records = []
    for day in entries:
        i = calendar.index(day)
        due = calendar[i + 6]
        days[day] = {}
        for code in candidates[day]:
            bars = [prices[d].get(code) for d in calendar[i : i + 7]]
            if any(
                not b
                or not b.get("C")
                or D(str(b["C"])) <= 0
                or D(str(b["AdjFactor"])) != 1
                or b["ExRT"] not in ("", None)
                for b in bars
            ):
                days[day][code] = None
                continue
            if any(not b.get("O") or D(str(b["O"])) <= 0 for b in (bars[0], bars[-1])):
                days[day][code] = None
                continue
            days[day][code] = value(
                dict(code=code, entry=day, entry_price=D(str(bars[0]["O"])), due=due),
                D(str(bars[-1]["O"])),
                due,
                calendar,
                fees,
                True,
            )
    for codes in itertools.product(*(candidates[d] for d in entries)):
        if any(not days[d][c] for d, c in zip(entries, codes, strict=True)):
            reasons["missing_price_or_rights"] += 1
            continue
        lots = [days[d][c] for d, c in zip(entries, codes, strict=True)]
        if any(
            a["code"] == b["code"] and b["entry"] < a["due"]
            for a, b in itertools.combinations(lots, 2)
        ):
            reasons["same_position_already_open"] += 1
            continue
        # Use the same fill engine as the benchmark, including full capital reservation.
        packet = {
            d: {CLOCK: {"candidates": [{"code": c}]}} for d, c in zip(entries, codes, strict=True)
        }
        book = run_book("chart", CLOCK, calendar, packet, prices, fees)
        if len(book["closed_trades"]) != len(lots) or book["open_marks"]:
            reasons["not_fully_executed"] += 1
            continue
        net, relative = path_costs(lots)
        if any(
            pnl(quoted) != pnl(filled)
            for quoted, filled in zip(lots, book["closed_trades"], strict=True)
        ):
            raise ValueError("independent pricing mismatch")
        records.append(
            dict(
                codes=codes,
                net=net,
                mean_trade_pct=relative,
                missing_fee_days=sum(r["missing_reverse_stock_days"] for r in lots),
            )
        )
    (selected,) = [r for r in records if r["codes"] == actual]
    if (selected["net"], selected["mean_trade_pct"]) != path_costs(target):
        raise ValueError("original selected trades did not reproduce")

    def distribution(field):
        values = [r[field] for r in records]
        number = selected[field]
        return dict(
            selected=number,
            median=median(values),
            minimum=min(values),
            maximum=max(values),
            below=sum(v < number for v in values),
            equal=values.count(number),
            above=sum(v > number for v in values),
        )

    return dict(
        mode=mode,
        first=entries[0],
        last_exit=max(r["due"] for r in target),
        entries=entries,
        lots=len(target),
        total_paths=len(records) + sum(reasons.values()),
        comparable=len(records),
        excluded=dict(reasons),
        net_yen=distribution("net"),
        average_trade_pct=distribution("mean_trade_pct"),
        outcomes_by_day=days,
        selected=selected,
    ), records


def main():
    calendar, quotes, _, _, _ = get_history()
    sources = sf.read(OUT / "signals.json")
    books = sf.read(OUT / "books.json")
    fees, _ = reverse_fee_table()
    history_fees = dict(fees)
    for day in calendar:
        if "2026-10-07" <= day <= "2026-10-30":
            rows, _ = historical_jsf(day)
            for code, row in rows.items():
                fee = row["品貸料率（円）"].strip()
                if fee and (not fee.strip("*＊") or fee.replace(".", "", 1).isdigit()):
                    history_fees.setdefault((day, code), D(0) if not fee.strip("*＊") else D(fee))
    results = {}
    all_paths = {}
    for name, key, first, f in [
        ("original_prior_close_cut", "original", "2026-11-04", fees),
        ("us_sensitivity", "us", "2026-10-07", history_fees),
    ]:
        prices = {d: quotes[d] for d in calendar if first <= d <= THROUGH}
        results[name], all_paths[name] = evaluate(
            name, sources[key], books[name]["market"], calendar, prices, f
        )
    for file, content in [("result.json", results), ("paths.json", all_paths)]:
        sf.write(DEST / file, json.loads(json.dumps(content, default=str)))
    print(
        json.dumps(
            {k: {n: v for n, v in r.items() if n != "outcomes_by_day"} for k, r in results.items()},
            default=str,
        )
    )


if __name__ == "__main__":
    main()
