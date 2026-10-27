from decimal import Decimal as D

from trading_system.backtest.metrics import summarize


def test_metrics_use_net_trades_and_initial_capital_drawdown():
    curve = [
        {
            "at": at,
            "equity": D(equity),
            "cash": D("50"),
            "free_cash": D("40"),
            "restricted_cash": D("10"),
            "gross_exposure": D("30"),
            "benchmark_close": D(benchmark),
        }
        for at, equity, benchmark in [
            ("2020-12-30T07:00:00+00:00", "90", "100"),
            ("2020-12-31T07:00:00+00:00", "120", "101"),
            ("2021-01-04T07:00:00+00:00", "96", "102"),
        ]
    ]
    trades = [
        {"net_pnl": D("20"), "holding_sessions": 2, "direction": "long"},
        {"net_pnl": D("-10"), "holding_sessions": 4, "direction": "short"},
    ]
    costs = {"commission": D("2"), "borrow": D("1"), "reverse_fee": D("0")}
    result = summarize(curve, trades, D("100"), costs, D("0"), 252, D("100"))
    assert result["total_return"] == D("-0.04")
    assert result["max_drawdown"] == D("0.2")
    assert result["profit_factor"] == 2
    assert result["win_rate"] == D("0.5")
    assert result["average_holding_sessions"] == 3
    assert result["yearly_returns"] == {"2020": D("0.2"), "2021": D("-0.2")}
    assert result["long_net_pnl"] == 20
    assert result["short_net_pnl"] == -10
    assert result["topix_relative_return"] is None
    assert result["costs"] == costs
    # Final equity includes open-position marks; it need not equal closed-trade P&L.
    assert result["final_equity"] == 96


def test_one_observation_without_variance_has_no_sharpe_or_cagr():
    point = {
        "at": "2020-01-01T07:00:00+00:00",
        "equity": D("100"),
        "cash": D("100"),
        "free_cash": D("100"),
        "restricted_cash": D("0"),
        "gross_exposure": D("0"),
        "benchmark_close": D("100"),
    }
    result = summarize([point], [], D("100"), {}, D("0"), 252, D("100"))
    assert result["sharpe"] is None
    assert result["cagr"] is None
    assert result["profit_factor"] is None
    assert result["win_rate"] is None
