"""Cash-only daily simulation with frozen sizing, delayed settlement and explicit misses."""

import math
from collections import Counter
from dataclasses import asdict, dataclass, field


def good(x):
    return x is not None and math.isfinite(x) and x > 0


@dataclass
class Holding:
    qty: int
    cost: float
    entry: int
    mark: float
    sector: str
    stop: float | None
    peak: float
    pending: bool = False
    uncertain: bool = False


@dataclass
class Book:
    capital: int
    cash: float
    nav: float
    positions: dict = field(default_factory=dict)
    settlements: list = field(default_factory=list)
    peak: float = 0.0
    realized: float = 0.0
    turnover: float = 0.0
    fees: float = 0.0
    uncertain: bool = False

    @classmethod
    def start(cls, capital):
        return cls(capital, float(capital), float(capital), peak=float(capital))

    def power(self, i):
        return self.cash - sum(amount for due, amount in self.settlements if due > i)

    def dump(self):
        return asdict(self)

    @classmethod
    def restore(cls, r):
        return cls(**dict(r, positions={c: Holding(**h) for c, h in r["positions"].items()}))


def purchase_bar(bar, kind, limit, quote=None):
    if bar is None or not good(bar.O):
        return None, "missing_open"
    if bar.AdjFactor != 1 or (bar.ExRT or "") not in ("", "0"):
        return None, "entry_right"
    if kind == "continuation":
        return (quote, "quote_model") if good(quote) else (None, "no_received_continuation_quote")
    if bar.UL == 1 and bar.O == bar.L == bar.H:
        return None, "locked_upper"
    if kind == "limit":
        if bar.O < limit:
            return bar.O, "open_limit"
        if good(bar.L) and bar.L < limit:
            return limit, "intraday_limit_model"
        return None, "limit_unfilled"
    return bar.O, "open_model"


def session(book, i, day, previous, prices, rank, cfg, spec, confirmed=None):
    cash0 = book.cash
    nav0 = book.nav
    fills = []
    skips = Counter()
    rate = cfg["rate"] + spec.get("stress", 0)

    def charge(value):
        return cfg["fee"] + value * rate

    book.settlements = [(d, a) for d, a in book.settlements if d > i]
    held_at_open = set(book.positions)

    def sell(c, px, why):
        h = book.positions.pop(c)
        gross = px * h.qty
        fee = charge(gross)
        net = gross - fee
        book.cash += net
        book.fees += fee
        book.realized += net - h.cost
        book.turnover += gross
        book.settlements.append((i + 2, net))
        fills.append(
            dict(
                code=c,
                side="sell",
                qty=h.qty,
                price=px,
                fee=fee,
                pnl=net - h.cost,
                sector=h.sector,
                reason=why,
            )
        )

    for c, h in list(book.positions.items()):
        bar = prices.get(c)
        prev = previous.get(c)
        if bar is None or not good(bar.AdjFactor):
            h.uncertain = True
            book.uncertain = True
            skips["unpriced_holding"] += 1
            continue
        fac = bar.AdjFactor
        if (bar.ExRT or "") not in ("", "0", "1", "2"):
            h.uncertain = True
            book.uncertain = True
            skips["unresolved_right"] += 1
            continue
        if fac != 1:
            qty = h.qty / fac
            if bar.ExRT not in ("1", "2") or abs(round(qty) - qty) > 0.01:
                h.uncertain = True
                book.uncertain = True
                skips["unresolved_right"] += 1
                continue
            h.qty = round(qty)
            h.mark *= fac
            h.peak *= fac
            if h.stop is not None:
                h.stop *= fac
            if h.qty % 100:
                h.uncertain = True
                book.uncertain = True
                skips["odd_lot_right"] += 1
        if h.uncertain:
            continue
        horizon = spec["hold"]
        time_exit = i - h.entry >= horizon
        trend = spec.get("exit") == "trend" and prev and good(prev.ma20) and prev.C < prev.ma20
        due = h.pending or time_exit or trend
        if not good(bar.O) or (bar.LL == 1 and bar.O == bar.H == bar.L):
            if due or (h.stop and good(bar.L) and bar.L <= h.stop):
                h.pending = True
            skips["exit_unavailable" if h.pending else "no_trade_mark"] += 1
            continue
        if due:
            sell(c, bar.O, "pending_or_scheduled")
            continue
        if h.stop and bar.O <= h.stop:
            sell(c, bar.O, "stop_gap")
    # Determine orders from previous close, with expected debits reserved in rank order.
    forecast_cash = max(0, book.power(i))
    actual_cash = forecast_cash
    sector_counts = Counter(h.sector for h in book.positions.values())
    free_slots = cfg["slots"] - len(book.positions)
    for c in rank:
        prev = previous.get(c)
        if not prev or not good(prev.C):
            skips["missing_reference"] += 1
            continue
        if c in held_at_open:
            skips["already_held"] += 1
            continue
        if free_slots <= 0:
            skips["position_limit"] += 1
            continue
        cap_sector = spec.get("sector_slots", cfg["sector_slots"])
        if cap_sector and sector_counts[prev.S33] >= cap_sector:
            skips["sector_limit"] += 1
            continue
        budget = min(nav0 * cfg["position_fraction"], cfg["cap_yen"], forecast_cash)
        if "sector_fraction" in cfg:
            exposure = sum(h.qty * h.mark for h in book.positions.values() if h.sector == prev.S33)
            sector_budget = max(0, nav0 * cfg["sector_fraction"] - exposure)
            budget = min(budget, sector_budget)
        else:
            sector_budget = float("inf")
        qty = 100 * math.floor(max(0, budget - cfg["fee"]) / (100 * prev.C * (1 + rate)))
        if spec.get("risk"):
            if not good(prev.atr):
                skips["atr_missing"] += 1
                continue
            qty = min(
                qty,
                100 * math.floor(nav0 * cfg["risk_fraction"] / (100 * cfg["atr_stop"] * prev.atr)),
            )
        if qty < 100:
            skips["cash_or_position_budget"] += 1
            continue
        if spec.get("exit") in ("stop", "trail") and not good(prev.atr):
            skips["atr_missing"] += 1
            continue
        forecast_cash -= prev.C * qty + charge(prev.C * qty)
        free_slots -= 1
        sector_counts[prev.S33] += 1
        side = spec.get("entry", "open")
        evidence = (confirmed or {}).get(c)
        if side == "continuation" and evidence and qty > evidence[1]:
            skips["insufficient_displayed_quantity"] += 1
            continue
        px, why = purchase_bar(
            prices.get(c),
            side,
            prev.C * (1 - cfg["limit_discount"]),
            evidence[0] if evidence else None,
        )
        if px is None:
            skips[why] += 1
            continue
        value = px * qty
        fee = charge(value)
        if (
            value + fee
            > min(nav0 * cfg["position_fraction"], cfg["cap_yen"], actual_cash, sector_budget)
            + 1e-8
        ):
            skips["gap_over_budget"] += 1
            continue
        actual_cash -= value + fee
        book.cash -= value + fee
        book.fees += fee
        book.turnover += value
        multiple = cfg["atr_stop"] if spec.get("exit") == "stop" else cfg["atr_trail"]
        stop = px - multiple * prev.atr if spec.get("exit") in ("stop", "trail") else None
        book.positions[c] = Holding(qty, value + fee, i, px, prev.S33, stop, px)
        fills.append(
            dict(
                code=c,
                side="buy",
                qty=qty,
                price=px,
                fee=fee,
                pnl=None,
                sector=prev.S33,
                reason=why,
            )
        )
    stale = 0
    for c, h in list(book.positions.items()):
        bar = prices.get(c)
        if bar is None or not good(bar.C) or h.uncertain:
            stale += 1
            continue
        if h.stop and good(bar.L) and bar.L <= h.stop and not (bar.LL == 1 and bar.H == bar.L):
            if h.entry == i and spec.get("entry", "open") != "open":
                book.uncertain = True
                skips["intraday_sequence_unknown"] += 1
            else:
                sell(c, h.stop, "stop_touch_model")
                continue
        h.mark = bar.C
        if spec.get("exit") == "trail" and good(bar.atr):
            h.peak = max(h.peak, bar.H)
            h.stop = max(h.stop or 0, h.peak - cfg["atr_trail"] * bar.atr)
    market = sum(h.qty * h.mark for h in book.positions.values())
    book.nav = book.cash + market
    book.peak = max(book.peak, book.nav)
    assert book.power(i) >= -0.001 and all(f["qty"] % 100 == 0 for f in fills)
    assert (
        abs(
            book.cash
            - cash0
            - sum(
                (1 if f["side"] == "sell" else -1) * f["price"] * f["qty"] - f["fee"] for f in fills
            )
        )
        < 0.01
    )
    return (
        dict(
            date=day,
            nav=book.nav,
            cash=book.cash,
            buying_power=book.power(i),
            market=market,
            gross_ex_cost=book.nav - book.capital + book.fees,
            fees=book.fees,
            realized=book.realized,
            positions=len(book.positions),
            stale=stale,
            uncertain=book.uncertain,
            turnover=book.turnover,
        ),
        fills,
        dict(skips),
    )
