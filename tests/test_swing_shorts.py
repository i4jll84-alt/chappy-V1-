import importlib.util
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from trading_system.research import swing_short_sources as src
from trading_system.research import swing_shorts as s


def test_credit_data_and_shares_do_not_conflate_markets():
    code = "12340"

    def js(loans, stock):
        return {code: {"融資残高株数": loans, "貸株残高株数": stock}}

    loan = {"a": js("12", "4"), "b": js("14", "3")}
    ex = [{code: {"LongVol": 20, "ShrtVol": 5, "Date": "old"}}, {}]
    r = s.credit_details({"code": code, "turnover_multiple": "1"}, loan, ["a", "b"], ex, ["p", "q"])
    assert r["loan_ratios"][0] == "3" and r["exchange_ratios"] == ["4", None]
    assert r["exclusions"] == ["stock_borrow_increased"]
    for n, d in [("1", "0"), ("", 3), (None, 3)]:
        assert s.ratio(n, d) is None
    loan["a"] = js("12", "0")
    assert (
        "loan_ratio_or_change_missing"
        in s.credit_details(
            {"code": code, "turnover_multiple": "1"}, loan, ["a", "b"], ex, ["p", "q"]
        )["exclusions"]
    )


def test_nested_research_groups_require_fresh_data_and_no_new_orders():
    rows = [
        {"code": str(i), "credit": {"exclusions": [] if i == 0 else ["squeeze"]}} for i in range(2)
    ]
    series = {
        k: {"state": "usable", "change": "-1" if k != "NK225F" else "1"} for k in s.EQUITY_INDICES
    }
    assert s.market_check({"series": series}) == "pass"
    series["NK225F"]["state"] = "stale"
    state = s.market_check({"series": series})
    assert state == "market_data_missing"
    packet = dict(state="available", candidates=rows, market_state=state)
    assert [s.choices(packet, k) for k in s.ARMS] == [["0", "1"], ["0"], []]
    pre = dict(short_research=packet, stopped=False)
    assert s.review(pre, dict(reason="risk", watch={"0": "weak"}))["state"] == "invalid_short_watch"
    packet["market_state"] = "pass"
    assert s.review(pre, dict(reason="risk", watch={"0": "weak"}))["watch"] == ["0"]
    assert (
        s.review(dict(pre, stopped=True), dict(reason="risk", watch={"0": "weak"}))["watch"] == []
    )


def test_short_prices_costs_and_carry_interval():
    path = Path(__file__).resolve().parents[1] / "scripts/swing_short_replay.py"
    spec = importlib.util.spec_from_file_location("short_replay_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dates = [
        (date(2026, 11, 2) + timedelta(days=i)).isoformat()
        for i in (0, 2, 3, 4, 7, 8, 9, 10, 11, 14)
    ]
    lot = dict(code="12340", entry=dates[0], entry_price=D(100), due=dates[6])
    rev = {(d, "12340"): D("0.1") for d in dates[:6]}
    x = mod.value(lot, D(110), dates[6], dates, rev, True)
    assert x["gross"] == -1000 and x["cost"] == D("30.50")
    assert x["known_reverse"] == 60 and x["missing_reverse_stock_days"] == 0
    assert x["borrow"] == D(10000) * D(".011") * 9 / 365
    assert mod.value(lot, D(90), dates[1], dates, {}, False)["missing_reverse_stock_days"] == 2


def test_restrictions_read_only_observed_rows(tmp_path):
    import hashlib

    root = tmp_path / "data/free-market/public"
    root.mkdir(parents=True)
    with sqlite3.connect(root / "public.sqlite") as db:
        db.execute(
            "create table observations (id integer primary key,url,state,"
            "raw_path,sha256,available_at)"
        )
        for i, (url, data) in enumerate(
            [
                (
                    src.JSF + "meigara.csv",
                    "見出し\n日,コード,名,貸借銘柄区分（東証）\n20261104,1234,Ａ,1\n20261104,1235,Ｂ,1\n20261104,1236,Ｃ,2",
                ),
                (src.JSF + "seigenichiran.csv", "規制\n直近,銘柄コード,銘柄名\n"),
                (src.SHORT_PAGE + "att/20261102_Stocks_Restricted_Nextday.csv", "1235,Ｂ\n"),
            ]
        ):
            body = data.encode("cp932")
            file = root / (str(i) + ".csv")
            file.write_bytes(body)
            db.execute(
                "insert into observations values (?,?,?,?,?,?)",
                (
                    i,
                    url,
                    "complete",
                    file.name,
                    hashlib.sha256(body).hexdigest(),
                    "2026-11-03T00:00:00+00:00",
                ),
            )
    for d, expected in [("2026-11-02", None), ("2026-11-04", {"12340"})]:
        reader = src.PublicInputs(tmp_path, datetime.fromisoformat(d + "T07:55:00+09:00"))
        try:
            assert reader.restrictions("2026-11-02", "2026-11-04")[0] == expected
        finally:
            reader.db.close()
