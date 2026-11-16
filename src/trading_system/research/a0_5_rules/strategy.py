from collections import Counter
from dataclasses import dataclass
from math import floor, isfinite

import pandas as pd

from .config import ATR_PERIOD, ATR_STOP_MULTIPLIER, MAX_POSITION_WEIGHT_PCT, RISK_PER_TRADE_PCT


@dataclass
class Position:
    shares: float
    entry: pd.Timestamp
    cost: float
    mark: float
    pending_exit: str = ""
    sector33: str | None = None
    initial_stop: float | None = None
    entry_signal_date: pd.Timestamp | None = None


ORDER_COLUMNS = [
    "signal_date",
    "order_date",
    "side",
    "order_type",
    "Code",
    "company",
    "shares",
    "reason",
    "reference_close",
    "volume_ratio",
    "margin_days",
    "financial_release",
    "margin_release",
    "flow_release",
    "S33",
    "avg_trading_value_20",
    "margin_reported_date",
    "is_published_date_estimated",
]


def sector_code(value):
    if pd.isna(value):
        return None
    return str(value).strip() or None


def order_columns(cfg):
    return [*ORDER_COLUMNS, "signal_atr"] if cfg.use_atr_risk_management else ORDER_COLUMNS


def atr_quantity(equity, close, atr, lot):
    if pd.isna(atr) or not isfinite(atr) or atr <= 0:
        return 0
    risk_lots = floor(equity * RISK_PER_TRADE_PCT / (ATR_STOP_MULTIPLIER * atr) / lot)
    weight_lots = floor(equity * MAX_POSITION_WEIGHT_PCT / close / lot)
    return min(risk_lots, weight_lots) * lot


def trend_signal(day, mode):
    """Filter the established buy signal using prices in signal-day share units."""
    if mode == "NONE":
        return day.Signal
    if mode == "ABOVE_SMA200":
        return day.Signal & day.C.gt(day.SMA200)
    if mode == "SMA50_ABOVE_SMA200":
        return day.Signal & day.C.gt(day.SMA50) & day.SMA50.gt(day.SMA200)
    raise ValueError(f"Unknown trend filter: {mode}")


def market_entry_signal(day, cfg):
    signal = day.Signal
    if cfg.require_topix_above_sma75:
        signal = signal & day.TOPIX.gt(day.TOPIXSMA75)
    if cfg.require_foreigners_net_buy:
        # A0 already requires positive net buying. The explicit E2 gate uses the same publication.
        signal = signal & day.FlowDate.lt(day.Date) & day.FrgnBal.gt(0)
    return signal


def ranked_candidates(day):
    return day.loc[day.Signal].sort_values(["AvgTradingValue20", "Code"], ascending=[False, True])


def held_sector_counts(lookup, positions):
    counts = Counter()
    for code, pos in positions.items():
        current = sector_code(lookup.at[code, "S33"]) if code in lookup.index else None
        counts[current or pos.sector33] += 1
    return counts


def orders(day, signal_date, calendar, cash, positions, cfg):
    """Size at the close, without spending expected proceeds of tomorrow's sales."""
    i = calendar.get_loc(signal_date)
    if i + 1 >= len(calendar):
        raise ValueError("翌立会日が未取得です。")
    next_day = calendar[i + 1]
    lookup = day.set_index("Code")
    planned = []

    def add(side, code, qty, reason, row):
        planned.append(
            dict(
                zip(
                    ORDER_COLUMNS,
                    [
                        signal_date,
                        next_day,
                        side,
                        "寄付成行",
                        code,
                        row.get("CoName"),
                        qty,
                        reason,
                        row.get("C"),
                        row.get("VolumeRatio"),
                        row.get("MarginDays"),
                        row.get("FinDate"),
                        row.get("MarginDate"),
                        row.get("FlowDate"),
                        sector_code(row.get("S33")),
                        row.get("AvgTradingValue20"),
                        row.get("MarginPubDate"),
                        row.get("is_published_date_estimated"),
                    ],
                )
            )
        )
        if cfg.use_atr_risk_management and side == "BUY":
            planned[-1]["signal_atr"] = row[f"ATR_{ATR_PERIOD}"]

    for code, pos in sorted(positions.items()):
        row = lookup.loc[code] if code in lookup.index else {}
        held = i - calendar.searchsorted(pos.entry) + 1
        reason = pos.pending_exit
        if not reason and cfg.max_hold and held >= cfg.max_hold:
            reason = "保有期限"
        if not reason and row.get("C", float("nan")) < row.get("ExitMA", float("nan")):
            reason = "終値が移動平均割れ"
        if reason:
            add("SELL", code, pos.shares, reason, row)

    slots = max(0, cfg.slots - len(positions))
    if cfg.max_daily_entries:
        slots = min(slots, cfg.max_daily_entries)
    sectors = held_sector_counts(lookup, positions)
    equity = cash + sum(p.shares * p.mark for p in positions.values())
    remaining = cash
    candidates = ranked_candidates(
        day.loc[~day.Code.isin(positions) & day.AvgTradingValue20.notna()]
    )
    for row in candidates.to_dict("records"):
        if slots == 0:
            break
        sector = sector_code(row["S33"])
        if cfg.max_positions_per_sector and (
            sector is None or sectors[sector] + sectors[None] >= cfg.max_positions_per_sector
        ):
            continue
        estimated = row["C"] * (1 + cfg.slip_bps / 10000) * (1 + cfg.fee_bps / 10000)
        if not isfinite(estimated) or estimated <= 0:
            continue
        if cfg.use_atr_risk_management:
            qty = atr_quantity(equity, row["C"], row.get(f"ATR_{ATR_PERIOD}"), cfg.lot)
        else:
            qty = floor(min(equity / cfg.slots, remaining) / (estimated * cfg.lot)) * cfg.lot
        if qty > 0:
            add("BUY", row["Code"], qty, "高値更新・出来高増加", row)
            remaining -= qty * estimated
            slots -= 1
            sectors[sector] += 1
    return pd.DataFrame(planned, columns=order_columns(cfg))
