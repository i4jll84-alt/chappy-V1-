"""Frozen orders and cash-equity paper executions for C1. No data access or broker calls."""

from bisect import bisect_left
from collections import Counter
from decimal import Decimal, InvalidOperation
from math import isfinite

STRATEGY = "c1-pullback-guarded-v1"
BUY_MULTIPLIER = Decimal("1.001")
SELL_MULTIPLIER = Decimal("0.999")
FEE_RATE = Decimal("0.0005")
GAP_LIMIT = Decimal("1.02")
LOT = 100


class C1UnverifiedBar(ValueError):
    """A portfolio phase cannot be finalized from the available official prices."""

    def __init__(self, issues):
        self.issues = issues
        super().__init__(f"C1 unverified holding bars: {issues}")


def _no_trade(row):
    # The input adapter sets this only from an official zero-volume, all-null OHLC row.
    return row.get("no_trade") is True and all(
        row.get(field) is None for field in ("open", "high", "low", "close")
    )


def _decimal(value):
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def _positive(value):
    amount = _decimal(value)
    return amount if amount is not None and amount > 0 else None


def _session(sessions, day):
    index = bisect_left(sessions, day)
    if index == len(sessions) or sessions[index] != day:
        raise ValueError(f"C1 trading session unavailable: {day}")
    return index


def _cost(raw_price, shares, side):
    price = raw_price * (BUY_MULTIPLIER if side == "BUY" else SELL_MULTIPLIER)
    gross = price * shares
    fee = gross * FEE_RATE
    return price, fee, gross + fee if side == "BUY" else gross - fee


def initial_account(capital=3_000_000):
    capital = _positive(capital)
    if capital is None:
        raise ValueError("C1 starting capital must be positive and finite")
    return dict(
        strategy=STRATEGY,
        capital=float(capital),
        cash=float(capital),
        positions={},
        settlements=[],
        trades=[],
        skips=[],
        equity=[],
        realized_pnl=0.0,
        peak_nav=float(capital),
        open_date=None,
        last_date=None,
        open_bars={},
    )


def available_cash(account, day):
    """Only settled cash can fund a new entry. Sale proceeds are already in total cash."""
    locked = sum(
        (_decimal(s["amount"]) for s in account["settlements"] if s["date"] > day), Decimal(0)
    )
    return float(_decimal(account["cash"]) - locked)


def _earnings_between(earnings, code, start, end, *, include_start=True):
    dates = earnings.get(code)
    if dates is None:
        return None
    if isinstance(dates, str):
        raise ValueError("C1 earnings must be a list of ISO dates or None")
    if include_start:
        return any(start <= value <= end for value in dates)
    return any(start < value <= end for value in dates)


def freeze_plan(
    account,
    signal_date,
    order_date,
    ranked,
    marks,
    earnings,
    sessions,
    *,
    allow_entries=True,
    stop_atr=2.0,
):
    """Create next-open instructions from facts available at the signal cutoff."""
    idx = _session(sessions, signal_date)
    if idx + 1 >= len(sessions) or sessions[idx + 1] != order_date:
        raise ValueError("C1 order date must be the next trading session")
    if account["open_date"] or (account["last_date"] and account["last_date"] != signal_date):
        raise ValueError("C1 signal date and closed account must agree")
    multiple = _positive(stop_atr)
    if multiple is None:
        raise ValueError("C1 stop_atr must be positive")
    plan = dict(
        strategy=STRATEGY,
        signal_date=signal_date,
        order_date=order_date,
        stop_atr=float(multiple),
        buys=[],
        sells=[],
        skips=[],
    )
    held = account["positions"]
    for code, pos in held.items():
        row = marks.get(code, {})
        close = _positive(row.get("close"))
        sma = _positive(row.get("sma5"))
        if _earnings_between(earnings, code, signal_date, order_date, include_start=False):
            reason = "earnings"
        elif pos.get("pending_exit") == "earnings":
            reason = "earnings"
        elif pos.get("pending_exit") == "above_sma5" or (
            close is not None and sma is not None and close > sma
        ):
            reason = "above_sma5"
        elif pos.get("pending_exit") == "time_exit" or (
            idx - _session(sessions, pos["entry_date"]) + 1 >= 10
        ):
            reason = "time_exit"
        else:
            continue
        plan["sells"].append(
            dict(
                code=code,
                reason=reason,
                close=float(close) if close is not None else None,
                sma5=float(sma) if sma is not None else None,
            )
        )
    if not allow_entries:
        return plan
    if hasattr(ranked, "to_dict"):
        ranked = ranked.to_dict("records")
    rows = sorted(ranked, key=lambda r: (float(r["deviation"]), r["code"]))
    if not rows:
        return plan
    if any(not isfinite(float(r["deviation"])) for r in rows):
        raise ValueError("C1 candidate deviation must be finite")
    if len({r["code"] for r in rows}) != len(rows):
        raise ValueError("C1 candidate codes must be unique")
    if idx + 3 >= len(sessions):
        raise ValueError("C1 needs three entry-window trading sessions")
    nav = _decimal(account["equity"][-1]["nav"] if account["equity"] else account["capital"])
    free_cash = _decimal(available_cash(account, order_date))
    # A held stock cannot enter this list even if a same-day sale is scheduled.
    # Reserve the ranked names before checking earnings, ATR, affordability or the open.
    chosen = [r for r in rows if r["code"] not in held][:2]
    for rank, r in enumerate(chosen, 1):
        code = r["code"]
        close, atr = _positive(r.get("close")), _positive(r.get("atr14"))
        event = _earnings_between(earnings, code, order_date, sessions[idx + 3])
        reason = None
        shares = 0
        if event is None:
            reason = "unknown_earnings"
        elif event:
            reason = "earnings_window"
        elif atr is None:
            reason = "missing_atr"
        elif close is None:
            reason = "missing_signal_close"
        elif not r.get("sector"):
            reason = "missing_sector"
        else:
            budget = min(nav * Decimal("0.20"), free_cash)
            _, _, one_lot = _cost(close, LOT, "BUY")
            shares = max(0, int(budget // one_lot)) * LOT
            if not shares:
                reason = "insufficient_cash"
        if reason:
            plan["skips"].append(dict(code=code, date=order_date, rank=rank, reason=reason))
            continue
        free_cash -= _cost(close, shares, "BUY")[2]
        plan["buys"].append(
            dict(
                code=code,
                sector=r["sector"],
                close=float(close),
                atr14=float(atr),
                deviation=float(r["deviation"]),
                shares=shares,
                rank=rank,
            )
        )
    return plan


def _fork(account):
    # Histories are append-only. Copy the containers without copying every prior trade record.
    return dict(
        account,
        positions={c: dict(p) for c, p in account["positions"].items()},
        settlements=list(account["settlements"]),
        trades=list(account["trades"]),
        skips=list(account["skips"]),
        open_bars=dict(account["open_bars"]),
    )


def _record_sale(state, code, day, raw_price, reason, sessions):
    pos = state["positions"].pop(code)
    index = _session(sessions, day)
    if index + 2 >= len(sessions):
        raise ValueError("C1 sale requires T+2 trading session")
    price, fee, proceeds = _cost(raw_price, pos["shares"], "SELL")
    net = proceeds - _decimal(pos["cost"])
    state["cash"] = float(_decimal(state["cash"]) + proceeds)
    state["realized_pnl"] = float(_decimal(state["realized_pnl"]) + net)
    state["settlements"].append(dict(date=sessions[index + 2], amount=float(proceeds)))
    state["trades"].append(
        dict(
            side="SELL",
            date=day,
            code=code,
            sector=pos["sector"],
            reason=reason,
            shares=pos["shares"],
            raw_price=float(raw_price),
            price=float(price),
            fee=float(fee),
            net_pnl=float(net),
            cost=pos["cost"],
            stop=pos["stop"],
            stop_atr=pos["stop_atr"],
            entry_date=pos["entry_date"],
            exit_date=day,
            holding_sessions=(index - _session(sessions, pos["entry_date"]) + 1),
            settlement_date=sessions[index + 2],
        )
    )


def _corporate_action(row):
    factor = _positive(row.get("adj_factor", 1))
    flag = row.get("ex_rights")
    return factor, factor != 1 or flag not in (None, "", "0", 0)


def execute_open(account, date, plan, bars, sessions):
    """Book only opening quotes. Repeating an already applied phase is harmless."""
    if account["open_date"] == date or account["last_date"] == date:
        return account
    if account["open_date"] is not None or (account["last_date"] and account["last_date"] > date):
        raise ValueError("C1 prior session must be closed before processing a new open")
    index = _session(sessions, date)
    if (
        plan.get("strategy") != STRATEGY
        or plan["order_date"] != date
        or index < 1
        or (sessions[index - 1] != plan["signal_date"])
    ):
        raise ValueError("C1 frozen plan date/strategy mismatch")
    last_date = account["last_date"]
    if last_date and last_date != plan["signal_date"]:
        raise ValueError("C1 skipped a portfolio valuation session")
    state = _fork(account)
    state["settlements"] = [s for s in state["settlements"] if s["date"] > date]
    state["skips"].extend(plan["skips"])
    state["open_date"] = date
    state["open_bars"] = {}
    held = state["positions"]
    initially_held = set(held)
    sales = {order["code"]: order["reason"] for order in plan["sells"]}
    if len(sales) != len(plan["sells"]) or not sales.keys() <= initially_held:
        raise ValueError("C1 duplicated or mismatched frozen sale")
    for code, pos in list(held.items()):
        row = bars.get(code, {})
        factor, action = _corporate_action(row)
        if factor is None or (action and factor == 1):
            raise C1UnverifiedBar({code: ["unresolved corporate action"]})
        if factor != 1:
            qty = Decimal(pos["shares"]) / factor
            if qty != qty.to_integral_value():
                raise C1UnverifiedBar({code: ["fractional corporate action"]})
            pos.update(
                shares=int(qty),
                stop=float(_decimal(pos["stop"]) * factor),
                last_price=float(_decimal(pos["last_price"]) * factor),
            )
        opening = _positive(row.get("open"))
        if opening is None and not _no_trade(row):
            raise C1UnverifiedBar({code: ["open"]})
        state["open_bars"][code] = float(opening) if opening is not None else None
        reason = sales.get(code)
        if opening is None:
            if reason:
                pos["pending_exit"] = reason
                state["skips"].append(dict(date=date, code=code, reason="missing_exit_open"))
            continue
        if reason != "earnings" and opening <= _decimal(pos["stop"]):
            reason = "gap_stop"
        if reason:
            _record_sale(state, code, date, opening, reason, sessions)
    sectors = Counter(p["sector"] for p in held.values())
    buys = plan["buys"]
    if len(buys) > 2 or len({b["code"] for b in buys}) != len(buys):
        raise ValueError("C1 invalid frozen entry list")
    for buy in buys:
        code, sector, shares = buy["code"], buy["sector"], buy["shares"]
        row = bars.get(code, {})
        opening = _positive(row.get("open"))
        _, action = _corporate_action(row)
        if shares <= 0 or shares % LOT:
            raise ValueError("C1 frozen shares must be whole positive lots")
        reason = None
        if code in initially_held:
            reason = "already_held_or_sold_today"
        elif len(held) >= 5:
            reason = "position_limit"
        elif sectors[sector] >= 2:
            reason = "sector_limit"
        elif action:
            reason = "corporate_action"
        elif opening is None:
            reason = "missing_open"
        elif opening > _decimal(buy["close"]) * GAP_LIMIT:
            reason = "opening_gap"
        else:
            price, fee, cost = _cost(opening, shares, "BUY")
            if cost > _decimal(available_cash(state, date)):
                reason = "insufficient_cash"
        if reason:
            state["skips"].append(dict(code=code, date=date, rank=buy["rank"], reason=reason))
            continue
        state["cash"] = float(_decimal(state["cash"]) - cost)
        stop = opening - _decimal(plan["stop_atr"]) * _decimal(buy["atr14"])
        held[code] = dict(
            code=code,
            sector=sector,
            shares=shares,
            cost=float(cost),
            entry_date=date,
            entry_price=float(price),
            raw_entry_price=float(opening),
            entry_fee=float(fee),
            atr14=buy["atr14"],
            stop=float(stop),
            stop_atr=plan["stop_atr"],
            last_price=float(opening),
        )
        state["open_bars"][code] = float(opening)
        sectors[sector] += 1
        state["trades"].append(
            dict(
                side="BUY",
                date=date,
                entry_date=date,
                code=code,
                sector=sector,
                shares=shares,
                reason="pullback_entry",
                raw_price=float(opening),
                price=float(price),
                fee=float(fee),
                signal_date=plan["signal_date"],
                signal_close=buy["close"],
                deviation=buy["deviation"],
                rank=buy["rank"],
                stop=float(stop),
                atr14=buy["atr14"],
                stop_atr=plan["stop_atr"],
            )
        )
    return state


def execute_close(account, date, bars, sessions):
    """Process intraday stops for surviving positions and mark all remaining holdings."""
    if account["last_date"] == date:
        return account
    if account["open_date"] != date:
        raise ValueError("C1 must apply the open before the close")
    state = _fork(account)
    marks = Decimal(0)
    cost = Decimal(0)
    stale_marks = []
    unverified_stops = []
    no_trade_marks = []
    for code, pos in list(state["positions"].items()):
        row = bars.get(code, {})
        low = _positive(row.get("low"))
        close = _positive(row.get("close"))
        stop = _decimal(pos["stop"])
        opening = _positive(state["open_bars"].get(code))
        if opening is None and _no_trade(row):
            no_trade_marks.append(code)
            marks += _decimal(pos["last_price"]) * pos["shares"]
            cost += _decimal(pos["cost"])
            continue
        missing = [name for name, value in (("open", opening), ("low", low)) if value is None]
        if missing:
            raise C1UnverifiedBar({code: missing})
        if low > opening or (close is not None and low > close):
            raise C1UnverifiedBar({code: ["inconsistent_ohlc"]})
        if low <= stop:
            _record_sale(state, code, date, stop, "intraday_stop", sessions)
            continue
        if close is None:
            raise C1UnverifiedBar({code: ["close"]})
        pos["last_price"] = float(close)
        marks += _decimal(pos["last_price"]) * pos["shares"]
        cost += _decimal(pos["cost"])
    cash = _decimal(state["cash"])
    nav = cash + marks
    peak = max(nav, _decimal(state["peak_nav"]))
    spendable = available_cash(state, date)
    record = dict(
        date=date,
        nav=float(nav),
        cash=float(cash),
        available_cash=spendable,
        unsettled=float(cash - _decimal(spendable)),
        position_value=float(marks),
        realized_pnl=state["realized_pnl"],
        unrealized_pnl=float(marks - cost),
        drawdown=float(nav / peak - 1),
        n_positions=len(state["positions"]),
        stale_marks=stale_marks,
        unverified_stops=unverified_stops,
        no_trade_marks=no_trade_marks,
    )
    return dict(
        state,
        last_date=date,
        open_date=None,
        open_bars={},
        peak_nav=float(peak),
        equity=[*state["equity"], record],
    )


def execute_day(account, date, plan, bars, sessions):
    return execute_close(execute_open(account, date, plan, bars, sessions), date, bars, sessions)
