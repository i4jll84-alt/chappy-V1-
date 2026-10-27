import copy
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system import cli
from trading_system.backtest.benchmark import synthetic_scenario
from trading_system.backtest.engine import run_backtest
from trading_system.backtest.models import Scenario

D = Decimal


def scenario(closes=(99, 101, 100, 110), opens=None, holding=1, quantity=100):
    template = synthetic_scenario().model_dump(mode="json")
    first = template["sessions"][0]
    result = template | {"declared_codes": ["SYN_LONG"], "actions": [], "sessions": []}
    result["strategy"] = {
        "quantity": quantity,
        "holding_sessions": holding,
        "max_allocation_fraction": "1",
    }
    opens = opens or closes
    for index, (close, opening) in enumerate(zip(closes, opens, strict=True)):
        session = copy.deepcopy(first)
        day = date(2020, 1, 6) + timedelta(days=index)
        at = datetime.combine(day, datetime.min.time(), timezone.utc)
        closing = at + timedelta(hours=6, minutes=30)
        session.update(
            date=day.isoformat(),
            open_at=at.isoformat(),
            decision_at=(closing + timedelta(minutes=1)).isoformat(),
            universe=["SYN_LONG"],
            universe_known_at=at.isoformat(),
            benchmark_close="1000",
        )
        session["bars"] = [
            session["bars"][0]
            | {
                "open": str(opening) if opening is not None else None,
                "close": str(close),
                "close_at": closing.isoformat(),
                "known_at": closing.isoformat(),
            }
        ]
        session["rules"] = [
            session["rules"][0]
            | {
                "known_at": at.isoformat(),
                "slippage_ticks": 0,
                "commission_rate": "0",
                "minimum_commission": "0",
                "borrow_annual_rate": "0",
            }
        ]
        result["sessions"].append(session)
    return result


def shift_session(session, days):
    session["date"] = (date.fromisoformat(session["date"]) + timedelta(days=days)).isoformat()
    for field in ("open_at", "decision_at", "universe_known_at"):
        session[field] = (datetime.fromisoformat(session[field]) + timedelta(days=days)).isoformat()
    for bar in session["bars"]:
        for field in ("close_at", "known_at"):
            bar[field] = (datetime.fromisoformat(bar[field]) + timedelta(days=days)).isoformat()
    for rule in session["rules"]:
        rule["known_at"] = (
            datetime.fromisoformat(rule["known_at"]) + timedelta(days=days)
        ).isoformat()


def action(source, kind, index, **terms):
    session = source["sessions"][index]
    return {
        "kind": kind,
        "code": "SYN_LONG",
        "effective_date": session["date"],
        "known_at": session["open_at"],
        **terms,
    }


def test_hand_calculated_long_ledger_and_no_same_close_execution():
    source = scenario(opens=(99, 101, 100, 110))
    for session in source["sessions"]:
        session["rules"][0]["commission_rate"] = "0.001"
    result = run_backtest(source)
    assert result["status"] == "complete"
    assert D(result["metrics"]["final_cash"]) == D("100979")
    assert D(result["trades"][0]["net_pnl"]) == D("979")
    assert D(result["metrics"]["costs"]["commission"]) == D("21")
    filled = [order for order in result["orders"] if order["status"] == "filled"]
    assert [order["side"] for order in filled] == ["buy", "sell"]
    assert filled[0]["requested_at"] == source["sessions"][1]["decision_at"]
    assert filled[0]["execution_at"] == source["sessions"][2]["open_at"]
    assert all(order["requested_at"] < order["execution_at"] for order in filled)


def test_short_carry_includes_weekend_reverse_fee_and_locked_proceeds():
    source = scenario((101, 100, 100, 90), (101, 100, 100, 90))
    shift_session(source["sessions"][2], 2)
    shift_session(source["sessions"][3], 4)  # Friday entry, Monday cover: three calendar days.
    for session in source["sessions"]:
        session["rules"][0].update(
            commission_rate="0.001", borrow_annual_rate="0.365", reverse_fee_per_share_per_day="0.1"
        )
    result = run_backtest(source)
    assert result["status"] == "complete"
    assert D(result["metrics"]["final_cash"]) == D("100921")
    assert D(result["trades"][0]["net_pnl"]) == D("921")
    assert D(result["metrics"]["costs"]["borrow"]) == D("30")
    assert D(result["metrics"]["costs"]["reverse_fee"]) == D("30")
    entry = result["equity_curve"][2]
    assert D(entry["cash"]) == D("109990")
    assert D(entry["restricted_cash"]) == D("20000")
    assert D(entry["free_cash"]) == D("89990")


def test_deterministic_full_input_hash_and_zero_metric_nulls():
    source = scenario((100, 100, 100, 100))
    first = run_backtest(source)
    assert first == run_backtest(copy.deepcopy(source))
    assert first["scenario"] == Scenario.model_validate(source).model_dump(mode="json")
    assert first["metrics"]["sharpe"] is None
    assert first["metrics"]["profit_factor"] is None
    assert first["metrics"]["win_rate"] is None
    assert first["metrics"]["topix_relative_return"] is None
    assert first["metrics"]["benchmark_kind"] == "synthetic_proxy"
    changed = copy.deepcopy(source)
    changed["strategy"]["holding_sessions"] = 2
    assert run_backtest(changed)["input_hash"] != first["input_hash"]
    json.dumps(first, allow_nan=False)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"can_buy": False}, "side_not_executable"),
        ({"opening_executable_quantity": 99}, "opening_liquidity_insufficient"),
        ({"lot_size": 200}, "invalid_trading_unit"),
        ({"limit_upper": "99"}, "execution_outside_price_band"),
    ],
)
def test_unfillable_entry_has_explicit_reason_and_no_trade(change, reason):
    source = scenario(opens=(99, 101, 100, 110))
    source["sessions"][2]["rules"][0].update(change)
    result = run_backtest(source)
    assert result["orders"][0]["status"] == "rejected"
    assert result["orders"][0]["reason"] == reason
    assert result["trades"] == []


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"short_allowed": False}, "short_sale_restricted"),
        ({"short_inventory": 99}, "short_inventory_insufficient"),
        ({"can_sell": False}, "side_not_executable"),
    ],
)
def test_short_inventory_and_restrictions_are_not_assumed(change, reason):
    source = scenario((101, 100, 100, 90))
    source["sessions"][2]["rules"][0].update(change)
    result = run_backtest(source)
    assert result["orders"][0]["reason"] == reason


def test_missing_open_rejects_without_filling_at_close():
    result = run_backtest(scenario(opens=(99, 101, None, 110)))
    assert result["orders"][0]["reason"] == "opening_price_unavailable"
    assert result["trades"] == []


def test_failed_exit_is_retried_and_terminal_position_is_not_force_liquidated():
    source = scenario((99, 101, 100, 101, 102))
    source["sessions"][3]["rules"][0]["can_sell"] = False
    result = run_backtest(source)
    assert result["orders"][1]["status"] == "rejected"
    assert result["orders"][2]["side"] == "sell" and result["orders"][2]["status"] == "filled"
    assert result["trades"][0]["holding_sessions"] == 2
    truncated = run_backtest(source | {"sessions": source["sessions"][:4]})
    assert len(truncated["positions"]) == 1
    assert truncated["orders"][-1]["status"] == "unfilled"
    assert truncated["orders"][-1]["reason"] == "end_of_supplied_sessions"


def test_universe_removal_blocks_entry_but_does_not_suppress_holding_exit():
    source = scenario()
    source["sessions"][2]["universe"] = []
    result = run_backtest(source)
    assert result["trades"][0]["holding_sessions"] == 1
    source["sessions"][1]["universe"] = []
    assert run_backtest(source)["trades"] == []


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("kind", ["dividend", "rights_cash"])
def test_signed_entitlement_offsets_ex_price_and_pays_later(side, kind):
    closes = (99, 100, 100, 98, 98, 98) if side == "long" else (101, 100, 100, 98, 98, 98)
    source = scenario(closes, holding=3)
    source["actions"] = [
        action(source, kind, 3, amount_per_share="2", pay_date=source["sessions"][4]["date"])
    ]
    result = run_backtest(source)
    assert result["status"] == "complete"
    at_ex = result["equity_curve"][3]
    assert D(at_ex["equity"]) == D("100000")
    assert D(at_ex["receivables"]) == D("200" if side == "long" else "-200")
    assert D(result["equity_curve"][4]["receivables"]) == 0
    assert D(result["metrics"]["final_equity"]) == D("100000")
    assert D(result["trades"][0]["net_pnl"]) == 0


@pytest.mark.parametrize(
    "ratio,quantity,new_quantity,prices",
    [
        ("2", 100, "200", (99, 100, 100, 50, 50, 50)),
        ("0.5", 200, "100", (99, 100, 100, 200, 200, 200)),
    ],
)
def test_split_and_consolidation_preserve_equity_and_historical_signal(
    ratio, quantity, new_quantity, prices
):
    source = scenario(prices, holding=3, quantity=quantity)
    source["actions"] = [action(source, "split", 3, ratio=ratio)]
    result = run_backtest(source)
    assert result["status"] == "complete"
    assert D(result["equity_curve"][3]["equity"]) == D("100000")
    assert D(result["trades"][0]["quantity"]) == D(new_quantity)
    assert D(result["trades"][0]["net_pnl"]) == 0
    assert [order["side"] for order in result["orders"]] == ["buy", "sell"]


def test_split_cancels_old_pending_quantity_and_resubmits_current_holding():
    source = scenario((99, 100, 100, 50, 50))
    source["actions"] = [action(source, "split", 3, ratio="2")]
    result = run_backtest(source)
    assert result["orders"][1]["status"] == "cancelled"
    assert result["orders"][2]["quantity"] == "200"
    assert result["orders"][2]["status"] == "filled"


def test_fractional_split_requires_explicit_settlement_and_odd_lot_exit_is_not_rounded():
    source = scenario((99, 100, 100, 300, 300), holding=2)
    source["actions"] = [action(source, "split", 3, ratio="0.333")]
    result = run_backtest(source)
    assert result["status"] == "blocked"
    assert result["reason"] == "unsupported_fractional_corporate_action"
    assert result["metrics"] is None
    assert all(order["status"] != "pending" for order in result["orders"])
    source["actions"][0]["ratio"] = "0.5"
    source["sessions"][3]["bars"][0].update(open="200", close="200")
    source["sessions"][4]["bars"][0].update(open="200", close="200")
    result = run_backtest(source)
    assert result["orders"][1]["quantity"] == "50"
    assert result["orders"][1]["reason"] == "invalid_trading_unit"
    assert result["positions"][0]["quantity"] == "50"


def test_adverse_tick_rounding_and_exact_short_collateral_check():
    source = scenario((101, 100, 100, 90), (101, 100, 100, 90))
    source["initial_cash"] = "9900"
    for session in source["sessions"]:
        session["rules"][0]["slippage_ticks"] = 1
    result = run_backtest(source)
    assert result["orders"][0]["status"] == "rejected"
    assert result["orders"][0]["reason"] in {"allocation_limit", "cash_or_collateral_insufficient"}
    assert result["status"] == "complete"
    source["initial_cash"] = "100000"
    source["sessions"][2]["bars"][0]["open"] = "100.2"
    source["sessions"][2]["rules"][0]["tick_size"] = "0.5"
    assert run_backtest(source)["orders"][0]["fill_price"] == "99.5"


def test_margin_breach_is_explicit_incomplete_simulation():
    source = scenario((101, 100, 100, 1000), holding=3)
    source["initial_cash"] = "10000"
    result = run_backtest(source)
    assert result["status"] == "blocked"
    assert result["reason"] == "unsupported_short_margin_breach"
    assert result["metrics"] is None


def test_future_information_cannot_change_earlier_order_requests():
    source = scenario()
    first = run_backtest(source)
    changed = copy.deepcopy(source)
    changed["sessions"][3]["bars"][0].update(open="500", close="700")
    changed["sessions"][3]["rules"][0]["opening_executable_quantity"] = 0
    second = run_backtest(changed)
    cutoff = source["sessions"][3]["decision_at"]

    def requests(result):
        return [
            {key: order[key] for key in ("id", "code", "side", "quantity", "requested_at")}
            for order in result["orders"]
            if order["requested_at"] < cutoff
        ]

    assert requests(first) == requests(second)
    source["sessions"][1]["bars"][0]["known_at"] = source["sessions"][2]["open_at"]
    assert run_backtest(source)["trades"] == []


def test_exact_availability_boundary_is_inclusive():
    source = scenario()
    source["sessions"][1]["bars"][0]["known_at"] = source["sessions"][1]["decision_at"]
    assert run_backtest(source)["orders"][0]["side"] == "buy"
    source["sessions"][1]["bars"][0]["known_at"] = (
        datetime.fromisoformat(source["sessions"][1]["decision_at"]) + timedelta(microseconds=1)
    ).isoformat()
    assert run_backtest(source)["trades"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(mode="historical"),
        lambda s: s["strategy"].update(quantity=True),
        lambda s: s["sessions"][0]["rules"][0].update(can_buy=1),
        lambda s: s["sessions"][0].update(date="2019-01-01"),
        lambda s: s["sessions"][0]["rules"][0].pop("short_allowed"),
        lambda s: s["sessions"][0].update(universe_known_at=s["sessions"][1]["open_at"]),
        lambda s: s["sessions"][0]["bars"][0].update(close="NaN"),
        lambda s: s.update(declared_codes=["FUTURE_CODE"]),
    ],
)
def test_invalid_or_unknown_assumptions_fail_closed(mutate):
    source = scenario()
    mutate(source)
    with pytest.raises(ValidationError):
        run_backtest(source)


def test_cli_synthetic_branch_requires_no_config_or_key_and_persists_input(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(cli.Settings, "load", lambda *_: pytest.fail("Acquisition config loaded"))
    monkeypatch.setattr(cli, "api_key", lambda: pytest.fail("API key requested"))
    output = tmp_path / "result.json"
    assert cli.main(["backtest-benchmark", "--output", str(output)]) == 0
    first = json.loads(output.read_bytes())
    assert first["status"] == "complete" and first["scenario"]["mode"] == "synthetic"
    capsys.readouterr()
    assert cli.main(["backtest-benchmark", "--scenario", str(output)]) == 2
    assert json.loads(output.read_bytes()) == first


@pytest.mark.parametrize("direction,expected", [("long", "100005"), ("short", "99995")])
def test_fractional_cash_in_lieu_has_exact_basis_and_signed_capital_pnl(direction, expected):
    closes = (
        (99, 100, 100, 200, 200, 200) if direction == "long" else (101, 100, 100, 200, 200, 200)
    )
    source = scenario(closes, holding=3, quantity=101)
    for session in source["sessions"]:
        session["rules"][0]["lot_size"] = 1
    source["actions"] = [action(source, "split", 3, ratio="0.5", cash_in_lieu_price="210")]
    result = run_backtest(source)
    assert result["status"] == "complete"
    assert D(result["metrics"]["final_equity"]) == D(expected)
    assert D(result["metrics"]["final_cash"]) == D(expected)
    assert D(result["trades"][0]["net_pnl"]) == D(expected) - D("100000")
    assert result["trades"][0]["quantity"] == "50"


def test_cash_or_allocation_limits_prevent_reusing_short_sale_proceeds():
    source = synthetic_scenario().model_dump(mode="json")
    source["initial_cash"] = "15000"
    source["strategy"]["max_allocation_fraction"] = "1"
    result = run_backtest(source)
    entry = [
        order
        for order in result["orders"]
        if datetime.fromisoformat(order["requested_at"])
        == datetime.fromisoformat(source["sessions"][1]["decision_at"])
    ]
    assert len(entry) == 2
    assert sum(order["status"] == "filled" for order in entry) == 1
    assert any(order["reason"] == "cash_or_collateral_insufficient" for order in entry)


def test_concurrent_split_and_dividend_and_unknown_exercise_are_rejected():
    source = scenario((99, 100, 100, 50, 50))
    source["actions"] = [
        action(source, "split", 3, ratio="2"),
        action(source, "dividend", 3, amount_per_share="1", pay_date=source["sessions"][4]["date"]),
    ]
    with pytest.raises(ValidationError, match="Concurrent"):
        run_backtest(source)
    source["actions"] = [action(source, "rights_exercise", 3)]
    with pytest.raises(ValidationError):
        run_backtest(source)


def test_hold_periods_cover_short_swing_and_medium_horizons():
    for holding in (5, 6, 21):
        source = scenario([99, 100] + [100] * (holding + 1), holding=holding)
        result = run_backtest(source)
        assert result["trades"][0]["holding_sessions"] == holding


def test_unavailable_holding_mark_cannot_indirectly_drive_margin_or_strategy():
    source = scenario((101, 100, 100, 90), holding=3)
    source["initial_cash"] = "10000"
    source["sessions"][2]["bars"][0]["known_at"] = source["sessions"][3]["open_at"]
    first = run_backtest(source)
    source["sessions"][2]["bars"][0]["close"] = "999"
    second = run_backtest(source)
    for result in (first, second):
        assert result["status"] == "blocked"
        assert result["reason"] == "held_position_close_unavailable"
        assert result["metrics"] is None
    assert first["orders"] == second["orders"]
    assert first["positions"] == second["positions"]
