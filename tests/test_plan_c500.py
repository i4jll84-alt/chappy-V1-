from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system.research import plan_c500 as c


def test_pre_rank_budget_then_fix_five_including_held_and_ties():
    def stock(code, close, momentum):
        return dict(
            code5=code,
            close=close,
            ret_20d=momentum,
            ma5=250,
            ma20=200,
            turnover=100_000_000,
            prev5_turnover_mean=90_000_000,
            atr14=8,
            jsf_loan=45,
            jsf_stock=10,
            prev_jsf_loan=45,
            prev_jsf_stock=10,
            jsf_net_balance=35,
            prev_jsf_net=35,
            jsf_restricted=False,
            no_split_20d=True,
        )

    frame = pd.DataFrame(
        [
            stock("00010", 2300, 2),
            stock("00020", 299, 1.5),
            *[stock(f"{i:04d}0", 1000, 1) for i in range(9, 2, -1)],
        ]
    )
    master = {r.code5: dict(MktNm="グロース", S33="5250") for r in frame.itertuples()}
    runner = SimpleNamespace(_is_domestic_common_stock=lambda *args: True)
    ranked = c.rank_candidates(frame, master, runner, 225000, set())
    assert [r["code"] for r in ranked] == [f"{i:04d}0" for i in range(3, 8)]
    assert len({r["shares"] for r in ranked}) == 1 and ranked[0]["shares"] == 200
    assert c.order_size(1000, 225000) == (1014, 200, 202911.4)
    held = [dict(code5=code, sector33_code="5250") for code in ("00030", "70000", "80000")]
    c.reserve(ranked, held, {"70000"}, 225000)
    assert [r["status"] for r in ranked] == [
        "held",
        "reserved",
        "sector_limit",
        "sector_limit",
        "sector_limit",
    ]
    ranked[2]["sector33_code"] = "3650"
    c.reserve(ranked, held, {"70000"}, 225000)
    assert ranked[2]["status"] == "cash"


def test_topix_exact_window_strict_equality_and_missing_session(tmp_path, monkeypatch):
    days = [str(date(2026, 1, 1) + timedelta(days=i)) for i in range(26)]
    frame = pd.DataFrame({"Date": days, "C": [1] + [100] * 25})
    monkeypatch.setattr(c.saved, "bulk", lambda *a: [frame])
    assert c.market_gate(tmp_path, days, None)["bullish"] is False
    frame.loc[25, "C"] = 101
    assert c.market_gate(tmp_path, days, None)["bullish"] is True
    frame.drop(index=18, inplace=True)
    with pytest.raises(ValueError, match="missing"):
        c.market_gate(tmp_path, days, None)


def test_bullish_order_to_native_fills_and_sector_survives_next_close(tmp_path):
    from pathlib import Path

    from trading_system.research import plan_b_v21 as v21
    from trading_system.research import v21_runtime

    project = Path(__file__).resolve().parents[1]
    if not (v21_runtime.engine_root(project) / "daily_runner.py").is_file():
        pytest.skip("local verified V21 engine is not installed")
    capital = 5_000_000
    signal, target = "2026-11-27", "2026-11-30"
    scratch = tmp_path / "engine"
    scratch.mkdir()
    native = v21_runtime.initial_native(project, scratch, capital)
    account = v21.snapshot(native, capital, {}, 0, signal)
    dates = [str(date(2026, 11, 7) + timedelta(days=i)) for i in range(21)]
    code = "12340"
    r = dict(
        code5=code,
        close=1000,
        ma5=980,
        ma20=950,
        ret_20d=0.15,
        turnover=110_000_000,
        prev5_turnover_mean=100_000_000,
        atr14=5,
        jsf_net_balance=5,
        prev_jsf_net=5,
        jsf_loan=10,
        jsf_stock=5,
        prev_jsf_loan=10,
        prev_jsf_stock=5,
        jsf_restricted=False,
        no_split_20d=True,
    )
    client = SimpleNamespace(
        fetch_listed_info=lambda day: pd.DataFrame(
            [dict(code5=code, MktNm="プライム", S33="0050", CoName="合成", ProdCat="011")]
        ),
        fetch_daily_quotes=lambda day: pd.DataFrame([dict(code5=code, AdjFactor=1)]),
    )
    runner = v21_runtime.native_runner(project, scratch, capital, native, client)
    runner.signal_rows = pd.DataFrame([r])
    policy = object.__new__(c.C500Entries)
    policy.account, policy.window, policy.target, policy.gate = (
        account,
        dates,
        target,
        dict(bullish=True),
    )
    history = pd.DataFrame(dict(code5=code, adj_factor=1, date=dates[:-1]))
    orders = []
    detail = policy.complete(runner, native, orders, True, set(), history)
    assert [(x["status"], x["rank"]) for x in detail["ranked"]] == [("reserved", 1)]
    assert native["cash"] == capital and native["pending_orders"][0]["remaining_shares"] == 200
    decision = dict(target=target, orders=orders, picks=detail["ranked"], native=native)
    result = v21.simulate(
        project,
        scratch,
        decision,
        capital,
        {code: dict(O=990, H=1005, L=989, C=1000, AdjFactor=1)},
        dict(account=account),
        False,
    )
    pos = result["account"]["positions"][code]
    assert (pos["shares"], pos["sector33_code"], pos["stop_price"]) == (200, "0050", 981.0)
    assert result["native"]["positions"][0]["sector33_code"] == "0050"
    assert result["account"]["cash"] == pytest.approx(capital - pos["cost"])
    assert result["account"]["nav"] == pytest.approx(result["account"]["cash"] + 200000)
