import csv
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from trading_system.brokers.mufg_esmart import account_inquiry as inquiry
from trading_system.plan_retirement import retire_for_v21
from trading_system.research import a0_5, plan_b_v21
from trading_system.research.swing_forward import read, write


def test_read_cash_account_without_persisting_identifiers():
    responses = iter(
        [
            {"ResultCode": 0, "Token": "dummy"},
            {"StockAccountWallet": 750000},
            [{"Symbol": "1111", "LeavesQty": 100}],
            [{"Symbol": "2222", "State": 3}, {"Symbol": "3333", "State": 5}],
        ]
    )
    calls = []

    def fixture(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, json=next(responses))

    result = inquiry.query("local", transport=httpx.MockTransport(fixture))
    assert result["status"] == "complete" and result["cash_buying_power_yen"] == 750000
    assert result["held_symbols"] == ["1111"] and result["open_order_symbols"] == ["2222"]
    assert calls == [
        ("POST", inquiry.BASE + "/token"),
        *(("GET", inquiry.BASE + p) for p in inquiry.ROUTES),
    ]
    assert "dummy" not in str(result)


def test_read_failure_never_claims_flat_or_sufficient():
    result = inquiry.query("local", transport=httpx.MockTransport(lambda _: httpx.Response(401)))
    assert result["status"] == "incomplete" and "held_symbols" not in result
    guard = inquiry.InquiryTransport(
        httpx.MockTransport(lambda _: pytest.fail("request escaped guard"))
    )
    with pytest.raises(inquiry.ProbeError):
        guard.handle_request(httpx.Request("POST", inquiry.BASE + "/sendorder"))


def test_retirement_is_recoverable_and_native_account_stays_off(tmp_path):
    root = tmp_path / "data/plan-forward"
    now = datetime.fromisoformat("2026-11-28T10:00:00+09:00")

    def spec(key, adapter, version, start):
        return dict(id=key, adapter=adapter, version=version, start=start, capital=3000000)

    old = spec("B", "plan_b_forward", "a0-5-guarded-v1", "2026-11-19")
    c = spec("C", "plan_c_pead_forward", "pead-dip3-atr4-h60-v1", "2026-11-25")
    new = spec("B", "plan_b_v21", "v21-opening-limit-atr-v1", "2026-11-27")
    plans = [dict(new, stage="active"), dict(c, stage="retired")]
    for account in (old, c):
        write(root / "accounts" / account["id"] / "enrollment.json", {"spec": account})
    retire_for_v21(tmp_path, root, plans, now)
    write(root / "accounts/B/enrollment.json", {"spec": new})
    retire_for_v21(tmp_path, root, plans, now)
    for account in (old, c):
        archive = root / "retired" / (account["id"] + "-" + account["version"])
        assert read(archive / "account/enrollment.json")["spec"] == account
        assert read(archive / "retirement.json")["files_sha256"]
    assert a0_5.opening(tmp_path, now, capital=3000000)["state"] == "retired"
    assert a0_5.active_capitals(tmp_path) == (1000000,)


def test_decision_crossing_open_drops_all_new_instructions(monkeypatch):
    prev = {"native": {"previous": True}}
    day = "2026-11-30"
    deadline = plan_b_v21.at(day, "09:00")
    monkeypatch.setattr(
        plan_b_v21.runtime,
        "morning",
        lambda *a: dict(credit_ready=True, native={"updated": True}, orders=[{}]),
    )
    decision = plan_b_v21.prepare(
        None,
        None,
        prev,
        3000000,
        ["2026-11-27"],
        day,
        deadline - timedelta(seconds=1),
        lambda: deadline,
    )
    assert decision["status"] == "missed_preopen" and not decision["picks"]
    assert decision["native"] == prev["native"] and not decision["orders"]


def test_unverified_split_requires_review_before_modeled_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(plan_b_v21.runtime, "native_runner", lambda *a: None)
    decision = dict(
        target="2026-11-30",
        native=dict(positions=[], pending_orders=[]),
        orders=[{"区分": "買い#1(指値・寄付)", "コード5桁": "12340"}],
    )
    bars = {"12340": {"AdjFactor": 0.5, "O": 100}}
    with pytest.raises(ValueError, match="corporate_action"):
        plan_b_v21.simulate(Path(), tmp_path, decision, 3000000, bars, {}, False)


def test_intraday_stops_follow_all_opening_fills(monkeypatch, tmp_path):
    class Runner:
        cfg = SimpleNamespace(atr_mult=2, state_path=tmp_path / "state.json")

        def sync_after_open(self, day, path):
            with path.open(encoding="utf8") as handle:
                fills = list(csv.DictReader(handle))
            assert [x["side"] for x in fills] == ["BUY", "BUY", "SELL", "SELL"]
            assert [x["seq"] for x in fills] == ["1", "2", "3", "4"]
            assert [x["exec_time"] for x in fills] == sorted(x["exec_time"] for x in fills)
            ledger = []
            balance = 100000.0
            for x in fills:
                price = float(x["price"])
                item = dict(
                    exec_id=x["exec_id"], side=x["side"], shares=100, price=price, code5=x["code"]
                )
                if x["side"] == "BUY":
                    balance -= 100 * price
                else:
                    item.update(allocated_cost=10000, realized_pnl=100 * price - 10000)
                    balance += 100 * price
                ledger.append(item)
            write(
                self.cfg.state_path,
                dict(cash=balance, cash_audit_hold=False, positions=[], trade_ledger=ledger),
            )

        def cancel_pending_orders(self, *args, **kwargs):
            pass

        def _calc_fee(self, value):
            return 0

    monkeypatch.setattr(plan_b_v21.runtime, "native_runner", lambda *args: Runner())
    codes = ["11110", "22220"]
    day = "2026-11-30"
    state = dict(
        positions=[],
        pending_orders=[dict(code5=c, selection_atr14=2, target_trade_date=day) for c in codes],
    )
    orders = [
        {
            "区分": "買い#1(指値・寄付)",
            "注文ID": c,
            "コード5桁": c,
            "株数": "100",
            "指値または逆指値": "100",
        }
        for c in codes
    ]
    bars = {c: dict(AdjFactor=1, O=100, L=50, C=99) for c in codes}
    result = plan_b_v21.simulate(
        Path(),
        tmp_path,
        dict(target=day, native=state, orders=orders),
        100000,
        bars,
        {"account": {"realized": 0}},
        False,
    )
    assert len(result["trades"]) == 2 and not result["account"]["positions"]
