"""Transparent research statistics; undefined quantities are null."""

import math
from datetime import datetime
from decimal import Decimal

ZERO = Decimal(0)
ONE = Decimal(1)


def summarize(
    curve, trades, initial_cash, costs, risk_free_annual_rate, sessions_per_year, benchmark_start
):
    if not curve:
        return None
    final = curve[-1]
    total_return = final["equity"] / initial_cash - ONE
    benchmark_return = final["benchmark_close"] / benchmark_start - ONE
    prior, peak, drawdown = initial_cash, initial_cash, ZERO
    returns = []
    yearly, year_start, previous_year_final = {}, initial_cash, initial_cash
    for point in curve:
        returns.append(point["equity"] / prior - ONE)
        prior = point["equity"]
        peak = max(peak, prior)
        drawdown = max(drawdown, ONE - prior / peak)
        year = point["at"][:4]
        if yearly and year not in yearly:
            year_start = previous_year_final
        yearly[year] = prior / year_start - ONE
        previous_year_final = prior
    excess = [value - risk_free_annual_rate / sessions_per_year for value in returns]
    mean = sum(excess, ZERO) / len(excess)
    variance = sum(((value - mean) ** 2 for value in excess), ZERO) / max(len(excess) - 1, 1)
    sharpe = mean / variance.sqrt() * Decimal(sessions_per_year).sqrt() if variance else None
    elapsed = datetime.fromisoformat(final["at"]) - datetime.fromisoformat(curve[0]["at"])
    years = Decimal(str(elapsed.total_seconds())) / Decimal(365 * 86400)
    cagr = None
    if years > 0 and final["equity"] > 0:
        try:
            approximate = math.pow(float(final["equity"] / initial_cash), float(ONE / years)) - 1
            cagr = Decimal(str(approximate)) if math.isfinite(approximate) else None
        except (ValueError, OverflowError):
            pass
    wins = [trade["net_pnl"] for trade in trades if trade["net_pnl"] > 0]
    losses = [trade["net_pnl"] for trade in trades if trade["net_pnl"] < 0]
    return {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "sharpe_convention": "sample_daily_excess_returns_annualized",
        "max_drawdown": drawdown,
        "benchmark_total_return": benchmark_return,
        "benchmark_kind": "synthetic_proxy",
        "topix_relative_return": None,
        "benchmark_excess_return": total_return - benchmark_return,
        "trade_count": len(trades),
        "profit_factor": sum(wins, ZERO) / -sum(losses, ZERO) if losses else None,
        "win_rate": Decimal(len(wins)) / len(trades) if trades else None,
        "average_win": sum(wins, ZERO) / len(wins) if wins else None,
        "average_loss": sum(losses, ZERO) / len(losses) if losses else None,
        "average_holding_sessions": Decimal(sum(t["holding_sessions"] for t in trades))
        / len(trades)
        if trades
        else None,
        "long_net_pnl": sum((t["net_pnl"] for t in trades if t["direction"] == "long"), ZERO),
        "short_net_pnl": sum((t["net_pnl"] for t in trades if t["direction"] == "short"), ZERO),
        "costs": costs,
        "yearly_returns": yearly,
        "final_cash": final["cash"],
        "final_equity": final["equity"],
        "accounting_cash_ratio": final["cash"] / final["equity"],
        "free_cash_ratio": final["free_cash"] / final["equity"],
        "restricted_cash": final["restricted_cash"],
        "gross_exposure_ratio": final["gross_exposure"] / final["equity"],
        "unrealized_positions_are_marked": True,
    }
