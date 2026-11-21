import numpy as np
import pandas as pd
import pytest

from trading_system.research import c1_execution as book
from trading_system.research import c1_signals as sig


def sample(count=3):
    days = pd.bdate_range("2026-07-01", periods=75)
    codes = [str(i * 10 + 10000) for i in range(count)]
    prices = np.arange(100, 175, dtype=float)
    prices[-3:] = [169, 167, 164]
    size = len(days) * count
    close = np.repeat(prices, count)
    bars = pd.DataFrame(
        dict(
            Date=np.repeat(days, count),
            Code=np.tile(codes, len(days)),
            O=close,
            H=close + 2,
            L=close - 2,
            C=close,
            Vo=np.full(size, 1000),
            Va=np.full(size, sig.MIN_AVG_VALUE),
            AdjFactor=1.0,
            ExRT=None,
        )
    )
    master = bars[["Date", "Code"]].assign(ProdCat="011", Mkt="0111", S33="3650")
    topix = pd.DataFrame(dict(Date=days, C=np.arange(200, 275, dtype=float)))
    margin = pd.DataFrame(
        dict(
            Date=days[-1] - pd.Timedelta(8, unit="D"), PubDate=days[-6], Code=codes, LongVol=5000.0
        )
    )
    return days, bars, master, topix, margin, {code: [] for code in codes}


def test_real_readiness_floor_2500_before_liquidity_and_margin_fraction():
    args = sample(2500)
    _, prices, master, _, margin, _ = args
    prices.loc[prices.Code.ne("10000"), "Va"] = 1
    # Technical coverage is identical even with no supplied earnings confirmations.
    result = sig.evaluate(*args[:5], earnings={})
    assert result["coverage_ok"] and result["allow_entries"]
    assert result["counts"]["data_ready"] == 2500 and result["counts"]["eligible"] == 1
    assert [r["code"] for r in result["ranked"]] == ["10000"]
    # Keep missing-balance names in the denominator; exactly 80% is sufficient for this gate.
    assert (
        "margin_coverage_below_floor"
        not in sig.evaluate(*args[:4], margin.iloc[:2000], args[5])["reasons"]
    )
    result = sig.evaluate(*args[:4], margin.iloc[:1999], args[5])
    assert {"margin_coverage_below_floor", "data_ready_below_2500"} <= set(result["reasons"])
    assert result["marks"]["10000"]["sma5"] is not None
    assert len(master[master.Date.eq(args[0][-1])]) == 2500


def test_rank_reservation_with_missing_earnings_and_atr_never_promotes_third(monkeypatch):
    args = sample()
    days, bars, _, _, _, earnings = args
    monkeypatch.setattr(sig, "MIN_COMPLETE", 1)
    del earnings["10000"]
    bars.loc[bars.Code.eq("10010") & bars.Date.eq(days[-1]), "H"] = np.nan
    result = sig.evaluate(*args)
    assert result["allow_entries"] and result["counts"]["data_ready"] == 2
    assert [r["code"] for r in result["ranked"]] == ["10000", "10010", "10020"]
    assert result["ranked"][1]["atr14"] is None
    cal = list(pd.bdate_range(days[0], periods=81).strftime("%Y-%m-%d"))
    frozen = book.freeze_plan(
        book.initial_account(),
        cal[74],
        cal[75],
        result["ranked"],
        result["marks"],
        result["earnings"],
        cal,
    )
    assert not frozen["buys"]
    assert [(r["code"], r["reason"]) for r in frozen["skips"]] == [
        ("10000", "unknown_earnings"),
        ("10010", "missing_atr"),
    ]


@pytest.mark.parametrize("change", ["factor", "rights", "missing_close", "product", "market"])
def test_comparison_window_and_membership_exclusions(monkeypatch, change):
    args = sample()
    days, bars, master, _, _, _ = args
    monkeypatch.setattr(sig, "MIN_COMPLETE", 1)
    if change in ("factor", "rights", "missing_close"):
        key, value = {
            "factor": ("AdjFactor", 0.5),
            "rights": ("ExRT", "2"),
            "missing_close": ("C", np.nan),
        }[change]
        bars.loc[bars.Date.eq(days[5]) & bars.Code.eq("10000"), key] = value
    else:
        key, val = ("ProdCat", "012") if change == "product" else ("Mkt", "0113")
        master.loc[master.Code.eq("10000"), key] = val
    assert [r["code"] for r in sig.evaluate(*args)["ranked"]] == ["10010", "10020"]


def test_missing_sector_remains_reserved(monkeypatch):
    args = sample()
    args[2].loc[args[2].Code.eq("10000"), "S33"] = None
    monkeypatch.setattr(sig, "MIN_COMPLETE", 1)
    result = sig.evaluate(*args)
    assert result["indicators"] == dict(rsi_method="Wilder", rsi_period=2, rsi_threshold=15)
    assert result["ranked"][0]["sector"] is None and result["counts"]["data_ready"] == 3
    cal = list(pd.bdate_range(args[0][0], periods=81).strftime("%Y-%m-%d"))
    frozen = book.freeze_plan(
        book.initial_account(),
        cal[74],
        cal[75],
        result["ranked"],
        result["marks"],
        result["earnings"],
        cal,
    )
    assert [r["code"] for r in frozen["buys"]] == ["10010"]
    assert frozen["skips"][0]["reason"] == "missing_sector"


def test_rsi_wilder_seed_and_boundary_rsi1501_fails(monkeypatch):
    assert sig.wilder_rsi2(np.array([[100.0], [101.0], [99.0], [98.0]]))[0] == pytest.approx(20)
    assert sig.wilder_rsi2(np.array([[1.0], [1.0], [1.0]]))[0] == 50
    args = sample()
    monkeypatch.setattr(sig, "MIN_COMPLETE", 1)
    monkeypatch.setattr(sig, "wilder_rsi2", lambda _: np.array([15.01, 15.0, 14.99]))
    assert [r["code"] for r in sig.evaluate(*args)["ranked"]] == ["10010", "10020"]


def test_cutoff_bars_credit_volume_and_topix_boundaries(monkeypatch):
    args = sample()
    days, bars, master, top, margin, earnings = args
    monkeypatch.setattr(sig, "MIN_COMPLETE", 1)
    bars.loc[bars.Date.eq(days[-1]), "Vo"] = 100_000
    margin.loc[margin.Code.eq("10010"), "LongVol"] = 5000.1
    embargo = margin.iloc[:1].assign(PubDate=days[-1] + pd.Timedelta(1, unit="D"), LongVol=99999)
    result = sig.evaluate(days, bars, master, top, pd.concat([margin, embargo]), earnings)
    assert [r["code"] for r in result["ranked"]] == ["10000", "10020"]
    top["C"] = 100
    result = sig.evaluate(*args)
    assert result["coverage_ok"] and not result["allow_entries"]
    assert result["reasons"] == ["topix_not_above_sma75"]
    assert not sig.evaluate(*args, enable_vol_circuit_breaker=True)["coverage_ok"]
    result = sig.evaluate(*args, enable_vol_circuit_breaker=True, vol_value=30)
    assert result["coverage_ok"] and "volatility_at_or_above_threshold" in result["reasons"]


def test_benchmark_gap_stock_marks_and_recency_reference(monkeypatch):
    args = sample(10)
    days, bars, master, top, margin, earnings = args
    monkeypatch.setattr(sig, "MIN_COMPLETE", 1)
    master = master.loc[~(master.Date.eq(days[-1]) & master.Code.isin(["10000", "10010", "10020"]))]
    res = sig.evaluate(days, bars, master, top.iloc[1:], margin, earnings)
    assert res["counts"]["margin_reference"] == 10
    assert res["counts"]["margin_floor"] == 8
    assert res["counts"]["market_universe"] == 7
    assert "topix_history_unavailable" in res["reasons"]
    assert "margin_coverage_below_floor" in res["reasons"]
    assert res["marks"]["10000"]["close"] == 164


@pytest.mark.parametrize("age,known", [(10, 3), (11, 0)])
def test_credit_freshness_ten_calendar_day_boundary(age, known):
    days, bars, master, top, margin, earnings = sample()
    margin["Date"] = days[-1] - pd.Timedelta(age, unit="D")
    margin["PubDate"] = days[-1] - pd.Timedelta(1, unit="D")
    assert (
        sig.evaluate(days, bars, master, top, margin, earnings)["counts"]["margin_known"] == known
    )
