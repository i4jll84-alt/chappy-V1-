"""Accounting and saved-input integrity checks for forward reports."""

import json
import math
from copy import deepcopy
from datetime import datetime

import pytest

from trading_system.research import forward_report_data as report
from trading_system.research import plan_a_forward

NOW = datetime.fromisoformat("2026-11-26T23:40:00+09:00")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def curve(values=(1000, 1100, 1100, 990), topix=(100, 102, 104, 103)):
    return [
        dict(date=day, equity=value, topix_close=market)
        for day, value, market in zip(
            ("2026-11-19", "2026-11-20", "2026-11-24", "2026-11-25"),
            values,
            topix,
            strict=True,
        )
    ]


def metrics(points, trades=(), start="2026-11-19", end="2026-11-25", fees=()):
    return report.period_metrics(
        points, trades, start=start, end=end, plan_start="2026-11-19", fees=fees
    )


def test_realized_daily_returns_include_zero_sessions_and_sample_sharpe():
    result = metrics(curve())
    assert [r["value"] for r in result["daily_returns"]] == pytest.approx([0.1, 0, -0.1])
    assert result["sharpe"] == pytest.approx(0, abs=1e-12)
    assert result["max_drawdown_pct"] == pytest.approx(-10)
    assert result["return_pct"] == pytest.approx(-1)
    assert result["topix_return_pct"] == pytest.approx(3)
    assert result["excess_points"] == pytest.approx(-4)


def test_period_uses_same_prior_close_for_strategy_and_topix():
    result = metrics(curve(), start="2026-11-23")
    assert result["baseline_date"] == "2026-11-20"
    assert result["return_pct"] == pytest.approx(-10)
    assert result["topix_return_pct"] == pytest.approx((103 / 102 - 1) * 100)
    assert result["daily_observations"] == 2


def test_new_account_has_own_baseline_no_prestart_history():
    points = curve()[2:]
    result = report.period_metrics(
        points, [], start="2026-01-01", end="2026-11-25", plan_start="2026-11-24"
    )
    assert result["start"] == "2026-11-24"
    assert result["baseline_date"] == "2026-11-24"
    assert result["daily_observations"] == 1
    assert result["sharpe"] is None
    assert result["topix_return_pct"] == pytest.approx((103 / 104 - 1) * 100)


def test_no_trades_is_defined_zero_but_undefined_sharpe_pf():
    result = metrics(curve((1000, 1000, 1000, 1000)))
    assert result["return_pct"] == result["max_drawdown_pct"] == 0
    assert result["trade_count"] == 0
    assert result["profit_factor"] is None
    assert result["sharpe"] is None


def test_pf_and_fees_use_closed_nets_once_and_period_filter():
    trades = [
        dict(exit_date="2026-11-20", net=178),
        dict(exit_date="2026-11-24", net=-238),
        dict(exit_date="2026-11-25", net=0),
    ]
    result = metrics(
        curve((1000, 1178, 940, 940)),
        trades,
        fees=[dict(date="2026-11-20", fee=42), dict(date="2026-11-24", fee=18)],
    )
    assert result["closed_profit_jpy"] == -60
    assert result["return_pct"] == pytest.approx(-6)
    assert result["profit_factor"] == pytest.approx(178 / 238)
    assert (result["trade_count"], result["wins"], result["losses"], result["breakeven"]) == (
        3,
        1,
        1,
        1,
    )
    assert result["fees"] == 60


def test_pf_without_losses_is_explicitly_undefined():
    result = metrics(curve(), [dict(exit_date="2026-11-20", net=100)])
    assert result["profit_factor"] is None
    assert result["profit_factor_reason"] == "no_closed_losses"


@pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf")])
def test_nonfinite_or_missing_number_rejected(bad):
    with pytest.raises(report.ReportInputError):
        report.number(bad)


def test_missing_endday_is_not_zero_performance():
    with pytest.raises(report.ReportInputError, match="end-of-period"):
        metrics(curve()[:-1])


def test_year_boundary_uses_prior_year_final_session():
    points = [
        dict(date="2026-12-30", equity=1000, topix_close=100),
        dict(date="2027-01-04", equity=1050, topix_close=101),
        dict(date="2027-01-05", equity=1050, topix_close=102),
    ]
    result = report.period_metrics(
        points, [], start="2027-01-01", end="2027-01-05", plan_start="2026-12-30"
    )
    assert result["baseline_date"] == "2026-12-30"
    assert result["return_pct"] == pytest.approx(5)
    assert result["daily_observations"] == 2


@pytest.fixture
def a_fixture(tmp_path):
    plan = dict(
        id="A",
        version="s3-jsf-morning-v1",
        adapter="plan_a_forward",
        start="2026-11-19",
        capital=3000000,
    )
    book = tmp_path / "data/plan-forward/accounts/A"
    previous = plan_a_forward.initial(plan["start"], plan["capital"])
    write(book / "initial.json", previous)
    for day, pnl in (("2026-11-20", 178), ("2026-11-24", -60)):
        buy_price, buy_cost, buy_fee, exit_price, exit_fee, net = (
            (100, 1010, 10, 120, 12, 178) if day.endswith("20") else (200, 2020, 20, 180, 18, -238)
        )
        decision = dict(
            target=day,
            signal_date="2026-11-19" if day.endswith("20") else "2026-11-20",
            recorded_at=day + "T08:00:00+09:00",
            cutoff=day + "T07:59:00+09:00",
        )
        state = deepcopy(previous)
        state.update(
            last_mark=day, realized=str(pnl), cash=str(3000000 + pnl), nav=str(3000000 + pnl)
        )
        close = dict(
            target=day,
            previous_sha256=report.logical_hash(previous),
            decision_sha256=report.logical_hash(decision),
            account=state,
            events=[dict(action="BUY", code="12340", date=day, cost=buy_fee)],
            trades=[
                dict(
                    is_sale=1,
                    code="12340",
                    entry_day=day,
                    exit_day=day,
                    shares_at_exit=10,
                    net=net,
                    entry_debit=buy_cost,
                    exit_proceeds=exit_price * 10 - exit_fee,
                    entry_raw_open=buy_price,
                    exit_price=exit_price,
                    exit_cost=exit_fee,
                )
            ],
            mark=dict(unrealized=0),
            preopen_stop_sha256=None,
        )
        write(book / "sessions" / day / "decision.json", decision)
        write(book / "sessions" / day / "close.json", close)
        previous = state
    return tmp_path, plan


def test_multiple_a_closes_net_matches_ledger_and_reader_is_read_only(a_fixture):
    root, plan = a_fixture
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    state, trades, fees, _ = report._a_account(
        report._Reader(root, NOW), plan, "2026-11-24", ["2026-11-20", "2026-11-24"]
    )
    assert float(state["realized"]) == -60
    assert sum(r["net"] for r in trades) == -60
    assert sum(r["fee"] for r in fees) == 60
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("kind", ["missing", "decision", "chain", "net"])
def test_a_missing_or_corrupted_saved_inputs_fail(a_fixture, kind):
    root, plan = a_fixture
    path = root / "data/plan-forward/accounts/A/sessions/2026-11-24/close.json"
    close = json.loads(path.read_bytes())
    if kind == "missing":
        path.unlink()
    else:
        if kind == "decision":
            close["decision_sha256"] = "bad"
        elif kind == "chain":
            close["previous_sha256"] = "bad"
        else:
            close["trades"][0]["net"] = 0
        write(path, close)
    with pytest.raises(report.ReportInputError):
        report._a_account(
            report._Reader(root, NOW), plan, "2026-11-24", ["2026-11-20", "2026-11-24"]
        )


def test_holding_current_price_is_saved_close_not_entry_quote():
    state = dict(
        positions={"12340": dict(entered="2026-11-20", quote=100, debit=1010, shares=10)},
        cash=2000,
        nav=3500,
        unrealized=490,
        last_mark="2026-11-26",
    )
    holdings = report._holdings(
        state,
        dict(adapter="plan_a_forward"),
        {"12340": dict(Date="2026-11-26", C=150)},
        {"12340": dict(CoName="試験")},
        "2026-11-26",
    )
    assert holdings[0]["entry_price"] == 100
    assert holdings[0]["current_price"] == 150
    assert holdings[0]["unrealized_jpy"] == 490
    assert holdings[0]["code"] == "1234"
    assert metrics(curve((1000, 1000, 1000, 1000)))["return_pct"] == 0


def test_missing_holding_close_is_not_substituted_with_entry():
    state = dict(
        positions={"12340": dict(entered="2026-11-20", quote=100, debit=1010, shares=10)},
        last_mark="2026-11-26",
    )
    with pytest.raises(report.ReportInputError, match="missing or stale"):
        report._holdings(state, dict(adapter="plan_a_forward"), {}, {}, "2026-11-26")


def test_decision_opening_deadline_is_strict():
    decision = dict(
        signal_date="2026-11-26",
        recorded_at="2026-11-27T09:00:00+09:00",
        cutoff="2026-11-27T08:59:00+09:00",
    )
    with pytest.raises(report.ReportInputError, match="deadline"):
        report._verify_decision(decision, "2026-11-27")


def test_reader_rejects_records_saved_after_report_clock(tmp_path):
    write(tmp_path / "future.json", dict(recorded_at="2026-11-27T00:00:00+09:00"))
    with pytest.raises(report.ReportInputError, match="available"):
        report._Reader(tmp_path, NOW).read(tmp_path / "future.json")


def test_one_day_statistics_do_not_manufacture_sharpe():
    result = metrics(curve(), start="2026-11-25")
    assert result["daily_observations"] == 1
    assert result["sharpe"] is None
    assert math.isfinite(result["return_pct"])


def test_week_closes_on_friday_without_waiting_for_sunday():
    flags = report.period_coverage(
        asof="2026-11-27",
        next_session="2026-11-30",
        period_start="2026-11-23",
        period_end="2026-11-29",
        plan_start="2026-11-19",
    )
    assert flags["period_closed"]
    assert not flags["partial"]


def test_year_closes_on_final_exchange_session():
    flags = report.period_coverage(
        asof="2026-12-30",
        next_session="2027-01-04",
        period_start="2026-01-01",
        period_end="2026-12-31",
        plan_start="2025-01-01",
    )
    assert flags["period_closed"]
    assert not flags["partial"]


def test_closed_year_with_new_account_still_discloses_short_history():
    flags = report.period_coverage(
        asof="2026-12-30",
        next_session="2027-01-04",
        period_start="2026-01-01",
        period_end="2026-12-31",
        plan_start="2026-11-19",
    )
    assert flags["period_closed"]
    assert flags["partial"]
    assert flags["short_history"]


def test_b_native_baseline_revision_hash_and_read_only(tmp_path):
    from trading_system.research import a0_5_book

    root = tmp_path / "data/a0-5-paper/accounts/3000000"
    state = a0_5_book.initial(3000000)
    a0_5_book.save(root, state)
    baseline = dict(
        start="2026-11-19",
        initial_capital=3000000,
        native_revision=state["revision"],
        native_revision_sha256=a0_5_book.revision_digest(root / "ledger.sqlite", state["revision"]),
        journal_offset=0,
    )
    write(tmp_path / "data/plan-forward/accounts/B/baseline.json", baseline)
    for day in ("2026-11-19", "2026-11-20"):
        state["plans"][day] = dict(
            date=day,
            order_date="2026-11-20" if day.endswith("19") else "2026-11-24",
            state="ready",
            equity=3000000,
            orders=[],
            recorded_at=day + "T18:00:00+09:00",
            cutoff=day + "T17:59:00+09:00",
        )
    state["last_open"] = "2026-11-20"
    a0_5_book.save(root, state)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    account, trades, fees, _ = report._b_account(
        report._Reader(tmp_path, NOW),
        dict(start="2026-11-19", capital=3000000),
        "2026-11-20",
        ["2026-11-20"],
    )
    assert account["realized"] == 0 and not trades and not fees
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    baseline["native_revision_sha256"] = "bad"
    write(tmp_path / "data/plan-forward/accounts/B/baseline.json", baseline)
    with pytest.raises(report.ReportInputError, match="baseline revision"):
        report._b_account(
            report._Reader(tmp_path, NOW),
            dict(start="2026-11-19", capital=3000000),
            "2026-11-20",
            ["2026-11-20"],
        )


def test_calendar_anchors_at_end_for_long_range(monkeypatch, tmp_path):
    from datetime import date, timedelta

    seen = []

    def saved(root, day, now):
        seen.append(day)
        return {str(day + timedelta(days=i)): "1" for i in range(-30, 21)}, []

    monkeypatch.setattr(report, "calendar_days", saved)
    days, next_day = report._calendar(report._Reader(tmp_path), "2026-11-19", "2026-12-13", NOW)
    assert seen[-1] == date(2026, 12, 13)
    assert next_day == "2026-12-14"
    assert "2026-12-13" in days


def test_late_missed_session_accepts_zero_buy_history(a_fixture):
    root, plan = a_fixture
    folder = root / "data/plan-forward/accounts/A/sessions/2026-11-24"
    decision = json.loads((folder / "decision.json").read_bytes())
    decision.pop("cutoff")
    decision.update(status="missed_preopen", picks=[], recorded_at="2026-11-24T10:00:00+09:00")
    close = json.loads((folder / "close.json").read_bytes())
    close.update(events=[], trades=[], decision_sha256=report.logical_hash(decision))
    close["account"].update(realized="178", cash="3000178", nav="3000178")
    write(folder / "decision.json", decision)
    write(folder / "close.json", close)
    state, trades, _, _ = report._a_account(
        report._Reader(root, NOW), plan, "2026-11-24", ["2026-11-20", "2026-11-24"]
    )
    assert float(state["realized"]) == 178
    assert len(trades) == 1
    close["events"] = [dict(action="BUY", code="12340", date="2026-11-24", cost=10)]
    write(folder / "close.json", close)
    with pytest.raises((KeyError, report.ReportInputError)):
        report._a_account(
            report._Reader(root, NOW), plan, "2026-11-24", ["2026-11-20", "2026-11-24"]
        )


def test_saved_preopen_cancellation_and_current_stop(tmp_path):
    plan = dict(id="A")
    target = "2026-11-27"
    folder = tmp_path / "data/plan-forward/accounts/A/sessions" / target
    decision = dict(recorded_at="2026-11-26T22:00:00+09:00")
    write(
        folder / "preopen_stop.json",
        dict(
            target=target,
            observed_at="2026-11-26T22:01:00+09:00",
            decision_sha256=report.logical_hash(decision),
        ),
    )
    assert "取消" in report._next_stop(report._Reader(tmp_path, NOW), plan, decision, target)
    (folder / "preopen_stop.json").unlink()
    write(tmp_path / "data/swing-forward/STOP", {})
    assert "停止" in report._next_stop(report._Reader(tmp_path, NOW), plan, decision, target)


def test_b_sqlite_decision_not_available_in_future():
    decision = dict(
        date="2026-11-26",
        recorded_at="2026-11-27T08:00:00+09:00",
        cutoff="2026-11-27T07:59:00+09:00",
    )
    with pytest.raises(report.ReportInputError, match="unavailable"):
        report._verify_decision(decision, "2026-11-27", now=NOW)


@pytest.fixture
def c_fixture(tmp_path):
    from trading_system.research.pead_source import PEAD_SOURCE_COMMIT
    from trading_system.research.plan_c_pead_forward import VERSION, account

    plan = dict(
        id="C", version=VERSION, adapter="plan_c_pead_forward", start="2026-11-25", capital=3000000
    )
    book = tmp_path / "data/plan-forward/accounts/C"
    write(
        book / "initial.json",
        dict(
            version=VERSION,
            source_commit=PEAD_SOURCE_COMMIT,
            strategy="GRID_DIP_H60_ATR40",
            start=plan["start"],
            initial_capital=plan["capital"],
        ),
    )
    native = dict(
        schema_version=1,
        account="plan_c_pead_v1",
        strategy="GRID_DIP_H60_ATR40",
        last_processed_date="",
        settled_cash=3000000.0,
        positions=[],
        unsettled=[],
        pending_buys=[],
        pending_target=None,
        alerts=[],
        plans={},
        receipts={},
        fills={},
        daily_journal=None,
        last_snapshot=None,
    )
    settled = deepcopy(native)
    settled.update(
        settled_cash=2999999.8,
        fills={
            "confirm:2026-11-26": [
                dict(
                    side="BUY",
                    code="12340",
                    date="2026-11-26",
                    shares=10,
                    price=100,
                    fee=10,
                    net_amount=1010,
                ),
                dict(
                    side="SELL",
                    code="12340",
                    date="2026-11-26",
                    shares=4,
                    price=120,
                    fee=4.8,
                    net_amount=475.2,
                ),
                dict(
                    side="SELL",
                    code="12340",
                    date="2026-11-26",
                    shares=6,
                    price=90,
                    fee=5.4,
                    net_amount=534.6,
                ),
            ]
        },
    )
    decision = dict(
        target="2026-11-26",
        signal_date="2026-11-25",
        recorded_at="2026-11-25T18:00:00+09:00",
        cutoff="2026-11-25T17:59:00+09:00",
        starting_account_sha256=report.logical_hash(native),
    )
    snapshot = dict(date="2026-11-26", bars={})
    close = dict(
        date="2026-11-26",
        version=VERSION,
        decision_sha256=report.logical_hash(decision),
        previous_native_sha256=report.logical_hash(native),
        native_sha256=report.logical_hash(settled),
        bars_sha256=report.logical_hash(snapshot),
        operator_state=settled,
        snapshot=snapshot,
        account=account(settled, plan["capital"], "2026-11-26", plan["capital"], 0),
        preopen_stop_sha256=None,
    )
    write(book / "sessions/2026-11-26/decision.json", decision)
    write(book / "sessions/2026-11-26/close.json", close)
    return tmp_path, plan, close


def test_c_partial_and_full_sale_match_cost_basis_without_pnl_field(c_fixture):
    root, plan, _ = c_fixture
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    state, closed, fees, _ = report._c_account(
        report._Reader(root, NOW), plan, "2026-11-26", ["2026-11-26"]
    )
    assert len(closed) == 2
    assert [r["net"] for r in closed] == pytest.approx([71.2, -71.4])
    assert sum(r["net"] for r in closed) == pytest.approx(-0.2)
    assert float(state["realized"]) == pytest.approx(-0.2)
    assert sum(r["fee"] for r in fees) == pytest.approx(20.2)
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("problem", ["unmatched_quantity", "entitlement"])
def test_c_unverified_cost_or_corporate_action_valuation_fails(c_fixture, problem):
    from trading_system.research.plan_c_pead_forward import account

    root, plan, close = c_fixture
    if problem == "unmatched_quantity":
        close["operator_state"]["fills"]["confirm:2026-11-26"][1]["shares"] = 11
    else:
        close["operator_state"]["paper_entitlement_review"] = [dict(code="12340", reason="spinoff")]
    close["native_sha256"] = report.logical_hash(close["operator_state"])
    close["account"] = account(
        close["operator_state"], plan["capital"], "2026-11-26", plan["capital"], 0
    )
    write(root / "data/plan-forward/accounts/C/sessions/2026-11-26/close.json", close)
    with pytest.raises(report.ReportInputError, match="basis|entitlement"):
        report._c_account(report._Reader(root, NOW), plan, "2026-11-26", ["2026-11-26"])


def test_missing_next_plan_preserves_valid_performance_and_holdings(monkeypatch, tmp_path):
    plans = [
        dict(
            id=key,
            stage="active",
            start="2026-11-19",
            capital=3000000,
            adapter="plan_a_forward",
            version="test",
            name=key,
        )
        for key in report.ACTIVE_IDS
    ]
    write(tmp_path / "config/forward-plans.json", dict(plans=plans))
    days = ["2026-11-19", "2026-11-20", "2026-11-24", "2026-11-25", "2026-11-26"]
    monkeypatch.setattr(report, "_calendar", lambda *args: (days, "2026-11-27"))
    monkeypatch.setattr(report, "_topix", lambda *args: dict.fromkeys(days, 100))
    monkeypatch.setattr(report, "dated_inputs", lambda *args: ({}, {}, []))

    def saved(*args):
        return (
            dict(
                positions={},
                cash=3000000,
                nav=3000000,
                unrealized=0,
                realized=0,
                last_mark="2026-11-26",
            ),
            [],
            [],
            [],
        )

    for name in ("_a_account", "_b_account", "_c_account"):
        monkeypatch.setattr(report, name, saved)
    write(
        tmp_path / "data/plan-forward/accounts/C/sessions/2026-11-26/close.json",
        dict(native_sha256="test"),
    )

    def missing(*args):
        raise FileNotFoundError("next decision absent")

    monkeypatch.setattr(report, "_planned", missing)
    packet = report.build_report_packet(tmp_path, "2026-11-26", now=NOW)
    assert packet["status"] == "partial"
    for plan in packet["plans"].values():
        assert plan["status"] == "partial"
        assert plan["history_status"] == "complete"
        assert plan["next_plan_status"] == "unavailable"
        assert plan["holdings"] == []
        assert plan["periods"]["inception"]["return_pct"] == 0
        assert "未確認" in plan["no_buy_reason"]


@pytest.mark.parametrize("where", ["account", "global", "common"])
def test_b_persisted_stop_survives_removed_switch(tmp_path, where):
    target = "2026-11-27"
    if where == "account":
        path = tmp_path / "data/a0-5-paper/accounts/3000000/stops" / (target + ".json")
        value = dict(observed_at="2026-11-26T23:00:00+09:00")
    elif where == "global":
        path = tmp_path / "data/a0-5-paper/stops" / (target + ".json")
        value = dict(observed_at="2026-11-26T23:00:00+09:00")
    else:
        path = tmp_path / "data/swing-forward/sessions" / target / "preopen.json"
        value = dict(target=target, stopped=True)
    write(path, value)
    reader = report._Reader(tmp_path, NOW)
    assert "停止" in report._next_stop(reader, dict(id="B", capital=3000000), {}, target)
    assert reader.manifest


def test_b_closed_trade_net_already_includes_entry_and_exit_fees(tmp_path):
    from trading_system.research import a0_5_book

    root = tmp_path / "data/a0-5-paper/accounts/3000000"
    state = a0_5_book.initial(3000000)
    a0_5_book.save(root, state)
    baseline = dict(
        start="2026-11-19",
        initial_capital=3000000,
        native_revision=state["revision"],
        native_revision_sha256=a0_5_book.revision_digest(root / "ledger.sqlite", state["revision"]),
        journal_offset=0,
    )
    write(tmp_path / "data/plan-forward/accounts/B/baseline.json", baseline)
    state["journal"] = [
        dict(
            Code="12340",
            side="BUY",
            date="2026-11-20",
            status="仮想約定",
            shares=10,
            price=100,
            fee=10,
            realized_pnl=None,
        ),
        dict(
            Code="12340",
            side="SELL",
            date="2026-11-24",
            status="仮想約定",
            shares=10,
            price=120,
            fee=12,
            realized_pnl=178,
        ),
    ]
    for day, target in (
        ("2026-11-19", "2026-11-20"),
        ("2026-11-20", "2026-11-24"),
        ("2026-11-24", "2026-11-25"),
    ):
        state["plans"][day] = dict(
            date=day,
            order_date=target,
            state="ready",
            equity=3000178 if day.endswith("24") else 3000000,
            orders=[],
            recorded_at=day + "T18:00:00+09:00",
            cutoff=day + "T17:59:00+09:00",
        )
    state.update(last_open="2026-11-24", cash=3000178)
    a0_5_book.save(root, state)
    account, trades, fees, _ = report._b_account(
        report._Reader(tmp_path, NOW),
        dict(start="2026-11-19", capital=3000000),
        "2026-11-24",
        ["2026-11-20", "2026-11-24"],
    )
    assert account["realized"] == 178
    assert trades[0]["net"] == 178
    assert trades[0]["fees"] == 22
    assert sum(row["fee"] for row in fees) == 22
    assert account["unrealized"] == 0


def test_source_change_during_read_is_explicit(tmp_path):
    path = tmp_path / "snapshot.json"
    write(path, dict(value=1))
    reader = report._Reader(tmp_path)
    reader.read(path)
    write(path, dict(value=2))
    with pytest.raises(report.ReportInputError, match="input changed"):
        reader.track(path)
