import json
import tempfile
from dataclasses import asdict, dataclass
from math import floor
from pathlib import Path

import pandas as pd

from .config import ATR_STOP_MULTIPLIER, RESULTS
from .data_processor import load_window
from .strategy import Position, order_columns, orders, sector_code

ATR_GAP = "初期逆指値・寄付"
ATR_TOUCH = "初期逆指値・ザラ場"


@dataclass
class Result:
    equity: pd.DataFrame
    executions: pd.DataFrame
    orders: pd.DataFrame
    positions: dict
    metrics: dict


def apply_action(pos, bar, lot):
    factor = bar.get("AdjFactor", 1)
    if pd.isna(factor) or factor == 1:
        return
    kind = str(bar.get("ExRT", ""))
    if kind not in ("1", "2"):
        raise ValueError("保有中に権利評価が必要な企業行動があり、評価を停止しました。")
    shares = pos.shares / factor
    # The API rounds factors (e.g. 0.333333). Accept only a whole-share result.
    if abs(shares - round(shares)) > 0.01 or round(shares) % lot:
        raise ValueError("分割・併合後の単元未満株の処分条件が不明のため評価を停止しました。")
    pos.mark *= factor
    if pos.initial_stop is not None:
        pos.initial_stop *= factor
    pos.shares = round(shares)


def sale(pos, base, cfg):
    price = base * (1 - cfg.slip_bps / 10000)
    value = price * pos.shares
    fee = value * cfg.fee_bps / 10000
    proceeds = value - fee
    return price, fee, proceeds, proceeds - pos.cost


def stop_sale(code, positions, date, base, reason, cash, cfg, records):
    pos = positions.pop(code)
    price, fee, proceeds, pnl = sale(pos, base, cfg)
    cash += proceeds
    records.append(
        {
            "signal_date": pos.entry_signal_date,
            "order_date": date,
            "side": "SELL",
            "order_type": "逆指値",
            "Code": code,
            "shares": pos.shares,
            "reason": reason,
            "S33": pos.sector33,
            "initial_stop": pos.initial_stop,
            "status": "約定",
            "filled_shares": pos.shares,
            "fill_price": price,
            "fee": fee,
            "realized_pnl": pnl,
            "cash_after": cash,
        }
    )
    return cash


def execute(planned, opening, date, cash, positions, cfg, records):
    gap_exits = set()
    for code, pos in list(positions.items()):
        bar = opening.get(code, {})
        apply_action(pos, bar, cfg.lot)
        base = bar.get("O", float("nan"))
        if (
            cfg.use_atr_risk_management
            and pos.initial_stop is not None
            and pd.notna(base)
            and 0 < base <= pos.initial_stop
        ):
            cash = stop_sale(code, positions, date, base, ATR_GAP, cash, cfg, records)
            gap_exits.add(code)
    for order in planned.to_dict("records"):
        code, side = order["Code"], order["side"]
        if side == "SELL" and code in gap_exits:
            continue  # The protective stop already sold these shares at this open.
        if date != order["order_date"]:
            raise ValueError("予定日と約定対象日が一致しません。")
        bar = opening.get(code, {})
        base = bar.get("O", float("nan"))
        qty = positions[code].shares if side == "SELL" else order["shares"]
        reason = ""
        price = fee = pnl = float("nan")
        if pd.isna(base) or base <= 0:
            reason = "始値なし"
        elif side == "BUY" and bar.get("AdjFactor") != 1:
            reason = "権利落日・新規注文失効"
        elif side == "BUY":
            price = base * (1 + cfg.slip_bps / 10000)
            fee = price * qty * cfg.fee_bps / 10000
            cost = price * qty + fee
            if cfg.use_atr_risk_management and cost > cash:
                lot_cost = price * cfg.lot * (1 + cfg.fee_bps / 10000)
                qty = min(qty, floor(cash / lot_cost) * cfg.lot)
                fee = price * qty * cfg.fee_bps / 10000
                cost = price * qty + fee
            if qty <= 0 or cost > cash:
                reason = "寄付資金不足"
            else:
                pos = Position(qty, date, cost, price, sector33=sector_code(order.get("S33")))
                if cfg.use_atr_risk_management:
                    atr = order["signal_atr"]
                    if pd.isna(atr) or not 0 < atr < float("inf"):
                        raise ValueError("買い注文に有効なシグナル日ATRがありません。")
                    pos.initial_stop = price - ATR_STOP_MULTIPLIER * atr
                    pos.entry_signal_date = order["signal_date"]
                    order["initial_stop"] = pos.initial_stop
                positions[code] = pos
                cash -= cost
        else:
            price, fee, proceeds, pnl = sale(positions.pop(code), base, cfg)
            cash += proceeds
        if reason and side == "SELL":
            positions[code].pending_exit = order["reason"]
        records.append(
            {
                **order,
                "status": reason or "約定",
                "filled_shares": 0 if reason else qty,
                "fill_price": float("nan") if reason else price,
                "fee": 0 if reason else fee,
                "realized_pnl": pnl,
                "cash_after": cash,
            }
        )
    return cash


def intraday_stops(by_code, date, cash, positions, cfg, records):
    for code, pos in list(positions.items()):
        if pos.initial_stop is None or code not in by_code.index:
            continue
        opening = by_code.at[code, "O"]
        if pd.isna(opening) or opening <= 0:
            continue
        # A new entry's slipped fill can leave its stop at or above the raw open.
        if opening <= pos.initial_stop:
            cash = stop_sale(code, positions, date, opening, ATR_GAP, cash, cfg, records)
        else:
            low = by_code.at[code, "L"]
            if pd.notna(low) and 0 < low <= pos.initial_stop:
                cash = stop_sale(
                    code, positions, date, pos.initial_stop, ATR_TOUCH, cash, cfg, records
                )
    return cash


def closed_holding_days(fills, calendar):
    """Exchange sessions from the actual buy open up to (excluding) the sell open."""
    entries, durations = {}, []
    for fill in fills.loc[fills.filled_shares.gt(0)].itertuples(index=False):
        session = calendar.get_loc(fill.order_date)
        if fill.side == "BUY":
            entries[fill.Code] = session
        else:
            durations.append(session - entries.pop(fill.Code))
    return durations


def simulate(windows, cfg):
    cash = float(cfg.capital)
    positions = {}
    planned = pd.DataFrame(columns=order_columns(cfg))
    executions, account = [], []
    anchor = None
    for table, calendar in windows:
        for date, day in table.groupby("Date", sort=True):
            # Only opening information is passed to the execution path.
            by_code = day.set_index("Code")
            needed = set(positions).union(planned.Code)
            subset = by_code.loc[by_code.index.isin(needed)]
            opening = subset[["O", "AdjFactor", "ExRT"]].to_dict("index")
            cash = execute(planned, opening, date, cash, positions, cfg, executions)
            if cfg.use_atr_risk_management:
                cash = intraday_stops(by_code, date, cash, positions, cfg, executions)
            stale, unlisted = 0, 0
            for code, pos in positions.items():
                if code not in by_code.index or not by_code.at[code, "Listed"]:
                    unlisted += 1
                    pos.mark = 0  # No verified terminal proceeds; retain the unresolved position.
                    pos.pending_exit = "上場情報なし・要確認"
                elif pd.notna(by_code.at[code, "C"]):
                    pos.mark = float(by_code.at[code, "C"])
                else:
                    stale += 1
                if code in by_code.index:
                    pos.sector33 = sector_code(by_code.at[code, "S33"]) or pos.sector33
            equity = cash + sum(pos.shares * pos.mark for pos in positions.values())
            index = day.TOPIX.iloc[0]
            if anchor is None:
                anchor = index
            domestic = day.loc[day.MarketUniverse]
            estimates = domestic.is_published_date_estimated.fillna(False)
            largest_sector = pd.Series(
                [p.sector33 for p in positions.values()], dtype="string"
            ).value_counts()
            account.append(
                {
                    "date": date,
                    "equity": equity,
                    "cash": cash,
                    "holdings": len(positions),
                    "topix_equity": cfg.capital * index / anchor,
                    "universe": len(domestic),
                    "margin_known": int(domestic.MarginKnown.sum()),
                    "financial_known": int(domestic.FinKnown.sum()),
                    "margin_actual_uses": int((domestic.MarginKnown & ~estimates).sum()),
                    "margin_estimated_uses": int((domestic.MarginKnown & estimates).sum()),
                    "max_sector_holdings": int(largest_sector.max()) if len(largest_sector) else 0,
                    "eligible": int(domestic.Eligible.sum()),
                    "signals": int(day.Signal.sum()),
                    "stale_marks": stale,
                    "unlisted_marks": unlisted,
                }
            )
            planned = orders(day, date, calendar, cash, positions, cfg)
    if not account:
        raise ValueError("対象期間に立会日がありません。")
    equity = pd.DataFrame(account)
    equity["drawdown"] = equity.equity / equity.equity.cummax().clip(lower=cfg.capital) - 1
    ledger_columns = order_columns(cfg) + (["initial_stop"] if cfg.use_atr_risk_management else [])
    fills = pd.DataFrame(
        executions,
        columns=[
            *ledger_columns,
            "status",
            "filled_shares",
            "fill_price",
            "fee",
            "realized_pnl",
            "cash_after",
        ],
    )
    closed = fills.realized_pnl.dropna()
    buys_by_date = (
        fills.loc[fills.side.eq("BUY") & fills.status.eq("約定")].groupby("order_date").size()
    )
    gross_profit = float(closed.clip(lower=0).sum())
    gross_loss = float(-closed.clip(upper=0).sum())
    durations = closed_holding_days(fills, calendar)
    metrics = {
        "average_holding_days": sum(durations) / len(durations) if durations else None,
        "sessions": len(equity),
        "eligible_sessions": int(equity.eligible.gt(0).sum()),
        "margin_actual_uses": int(equity.margin_actual_uses.sum()),
        "margin_estimated_uses": int(equity.margin_estimated_uses.sum()),
        "max_sector_holdings": int(equity.max_sector_holdings.max()),
        "max_daily_entries_filled": int(buys_by_date.max()) if len(buys_by_date) else 0,
        "missing_margin_sessions": int(equity.margin_known.eq(0).sum()),
        "return": equity.equity.iloc[-1] / cfg.capital - 1,
        "max_drawdown": equity.drawdown.min(),
        "topix_return": equity.topix_equity.iloc[-1] / cfg.capital - 1,
        "closed_trades": len(closed),
        "win_rate": float((closed > 0).mean()) if len(closed) else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "open_positions": len(positions),
        "unfilled": int(fills.status.ne("約定").sum()),
    }
    return Result(equity, fills, planned, positions, metrics)


def feature_windows(path, start, end, cfg):
    info = json.loads(path.read_text(encoding="utf-8"))
    calendar = pd.DatetimeIndex(info["sessions"])
    if str(start)[:10] < info["start"] or str(end)[:10] > info["end"]:
        raise ValueError("指定期間がスナップショットの範囲外です。")
    days = calendar[(calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))]
    # Bound memory on long histories; cash, pending orders and holdings cross chunk boundaries.
    for i in range(0, len(days), 63):
        yield load_window(path, days[i], days[min(i + 62, len(days) - 1)], cfg)


def backtest(path, start, end, cfg):
    return simulate(feature_windows(path, start, end, cfg), cfg)


def save_result(result, cfg, path, start, end, root=RESULTS):
    root.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=f"backtest-{str(end)[:10]}-", dir=root))
    for name in ("equity", "executions", "orders"):
        getattr(result, name).to_csv(out / (name + ".csv"), index=False, encoding="utf-8-sig")
    meta = {
        "snapshot": path.name,
        "start": str(start),
        "end": str(end),
        "parameters": asdict(cfg),
        "metrics": result.metrics,
        "positions": {code: asdict(pos) for code, pos in result.positions.items()},
    }
    (out / "run.json").write_text(
        json.dumps(meta, default=str, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return out
