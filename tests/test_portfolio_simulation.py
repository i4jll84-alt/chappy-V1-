import copy
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system import cli
from trading_system.portfolio.simulation import run_portfolio_simulation
from trading_system.portfolio.simulation_benchmark import synthetic_simulation

D = Decimal


def flat_source(direction=1, two_codes=False):
    data = synthetic_simulation().model_dump(mode="json")
    data["market"]["sessions"] = data["market"]["sessions"][:6]
    data["sessions"] = data["sessions"][:6]
    if not two_codes:
        data["market"]["declared_codes"] = ["SYN_LONG"]
    data["strategies"] = [
        {"strategy": "fast", "weight": "0.25", "priority": 1, "holding_sessions": 1},
        {"strategy": "slow", "weight": "0.25", "priority": 2, "holding_sessions": 3},
    ]
    for index, (market, frame) in enumerate(
        zip(data["market"]["sessions"], data["sessions"], strict=True)
    ):
        if not two_codes:
            market["bars"], market["rules"], market["universe"] = (
                market["bars"][:1],
                market["rules"][:1],
                ["SYN_LONG"],
            )
            frame["opening_instruments"] = frame["opening_instruments"][:1]
            frame["decision_instruments"] = frame["decision_instruments"][:1]
            frame["opening_correlations"], frame["decision_correlations"] = [], []
        for bar in market["bars"]:
            bar.update(open="100", close="100")
        for rule in market["rules"]:
            rule.update(
                commission_rate="0",
                minimum_commission="0",
                slippage_ticks=0,
                tick_size="1",
                borrow_annual_rate="0",
                reverse_fee_per_share_per_day="0",
            )
        for instrument in frame["opening_instruments"]:
            instrument.update(
                mark_price="100", planning_price="100", fee_fixed="0", fee_per_share="0"
            )
        for instrument in frame["decision_instruments"]:
            instrument.update(
                mark_price="100", planning_price="105", fee_fixed="0", fee_per_share="0"
            )
        template = frame["signals"][0]
        frame["signals"] = (
            []
            if index
            else [
                template | {"strategy": "fast", "direction": direction},
                template
                | {
                    "strategy": "slow",
                    "direction": direction,
                    "code": "SYN_SHORT" if two_codes else "SYN_LONG",
                },
            ]
        )
    data["limits"].update(
        max_symbol_fraction="0.9",
        max_sector_fraction="0.9",
        max_long_fraction="0.9",
        max_short_fraction="0.9",
        max_gross_fraction="2",
        min_cash_ratio="0",
        max_stress_loss_fraction="1",
        max_reference_loss_fraction="0.9",
    )
    return data


def one_strategy(source):
    source["strategies"] = source["strategies"][:1]
    for frame in source["sessions"]:
        frame["signals"] = [signal for signal in frame["signals"] if signal["strategy"] == "fast"]
    return source


def reprice(source, index, opening=None, close=None, code_index=0):
    market, frame = source["market"]["sessions"][index], source["sessions"][index]
    if opening is not None:
        market["bars"][code_index]["open"] = opening
        frame["opening_instruments"][code_index].update(mark_price=opening, planning_price=opening)
    if close is not None:
        market["bars"][code_index]["close"] = close
        frame["decision_instruments"][code_index].update(
            mark_price=close, planning_price=str(D(close) * D("1.05"))
        )


def test_benchmark_is_deterministic_flat_reconciled_and_input_unchanged():
    source = synthetic_simulation().model_dump(mode="json")
    untouched = copy.deepcopy(source)
    first = run_portfolio_simulation(source)
    assert first == run_portfolio_simulation(source)
    assert source == untouched
    assert first["status"] == "complete" and not first["execution_enabled"]
    assert first["metrics"]["all_positions_flat"] and first["metrics"]["accounting_reconciled"]
    assert abs(D(first["accounting_diagnostics"]["equity_residual"])) <= D("1e-24")
    assert first["accounting_diagnostics"]["commission_allocations_exact"]
    assert len(first["trades"]) == 5
    assert all(
        event["requested_at"] < event["execution_at"]
        for event in first["orders"]
        if event["status"] == "filled"
    )
    json.dumps(first, allow_nan=False)


def test_contributors_exit_on_their_own_horizons_and_partially_occupied_code_cannot_add():
    source = flat_source()
    source["sessions"][2]["signals"] = [
        source["sessions"][0]["signals"][0]
        | {
            "decision_at": source["sessions"][2]["decision_at"],
            "known_at": source["sessions"][2]["decision_at"],
            "feature_known_at": source["sessions"][2]["decision_at"],
        }
    ]
    result = run_portfolio_simulation(source)
    assert [(t["strategy"], t["holding_sessions"], t["quantity"]) for t in result["trades"]] == [
        ("fast", 1, 200),
        ("slow", 3, 200),
    ]
    assert sum(event["kind"] == "entry" for event in result["orders"]) == 1
    assert any(
        row["reason"] == "duplicate_existing_or_reserved_code"
        for row in result["plans"][2]["plan"]["rejections"]
    )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("can_buy", False, "side_not_executable"),
        ("opening_executable_quantity", 399, "opening_liquidity_insufficient"),
        ("limit_upper", "99", "execution_outside_price_band"),
    ],
)
def test_entry_is_fixed_all_or_none_single_attempt(field, value, reason):
    source = flat_source()
    source["market"]["sessions"][1]["rules"][0][field] = value
    result = run_portfolio_simulation(source)
    assert result["status"] == "complete" and result["positions"] == []
    assert len(result["orders"]) == 1
    assert result["orders"][0]["quantity"] == 400
    assert result["orders"][0]["reason"] == reason
    assert D(result["metrics"]["final_cash"]) == D("100000")
    assert result["ledger"] == []


def test_execution_lot_change_does_not_resize_queued_quantity():
    source = flat_source()
    source["market"]["sessions"][1]["rules"][0]["lot_size"] = 300
    source["sessions"][1]["opening_instruments"][0]["lot_size"] = 300
    result = run_portfolio_simulation(source)
    assert result["orders"][0]["quantity"] == 400
    assert result["orders"][0]["reason"] == "invalid_trading_unit"
    assert not result["positions"]


def test_adverse_tick_rounding_applies_only_at_later_open():
    source = one_strategy(flat_source())
    reprice(source, 1, opening="100.2")
    rule = source["market"]["sessions"][1]["rules"][0]
    rule.update(tick_size="0.5", slippage_ticks=1)
    result = run_portfolio_simulation(source)
    assert result["orders"][0]["status"] == "filled"
    assert D(result["orders"][0]["fill_price"]) == D("101")
    assert result["orders"][0]["quantity"] == 200
    assert datetime.fromisoformat(result["orders"][0]["execution_at"]) == datetime.fromisoformat(
        source["market"]["sessions"][1]["open_at"]
    )


def test_bad_opening_gap_is_risk_rechecked_without_mutating_cash():
    source = one_strategy(flat_source())
    reprice(source, 1, opening="200")
    result = run_portfolio_simulation(source)
    assert result["orders"][0]["reason"] == "opening_reservation_risk"
    assert result["orders"][0]["quantity"] == 200
    assert result["ledger"] == [] and result["positions"] == []
    assert result["metrics"]["final_cash"] == "100000"


def test_postcommission_equity_denominator_rejects_exact_full_budget_before_mutation():
    source = one_strategy(flat_source())
    source["strategies"][0]["weight"] = "0.2"
    source["sessions"][0]["decision_instruments"][0]["planning_price"] = "100"
    source["market"]["sessions"][1]["rules"][0]["minimum_commission"] = "1"
    result = run_portfolio_simulation(source)
    event = result["orders"][0]
    assert event["quantity"] == 200 and event["reason"] == "actual_post_fill_risk"
    assert "strategy_budget:fast" in event["risk_reasons"]
    assert [item["phase"] for item in result["opening_checks"]] == [
        "reservation",
        "actual_post_fill",
    ]
    assert result["ledger"] == [] and result["positions"] == []
    assert result["metrics"]["final_cash"] == "100000"


def test_adverse_short_sale_gap_has_explicit_conservative_cash_buffer():
    source = one_strategy(flat_source(-1))
    source["strategies"][0]["weight"] = "0.21"
    source["limits"]["min_cash_ratio"] = "0.7995"
    source["sessions"][0]["decision_instruments"][0]["planning_price"] = "100"
    source["market"]["sessions"][1]["rules"][0]["slippage_ticks"] = 1
    result = run_portfolio_simulation(source)
    assert result["orders"][0]["reason"] == "opening_reservation_risk"
    check = result["opening_checks"][0]
    assert D(check["short_slippage_buffer"]) == D("200")
    assert "minimum_cash_ratio" in check["plan"]["blocked_reasons"]
    assert result["ledger"] == [] and result["positions"] == []


def test_failed_contributor_exit_retries_without_releasing_position_or_cash():
    source = flat_source()
    source["market"]["sessions"][2]["rules"][0]["can_sell"] = False
    result = run_portfolio_simulation(source)
    failed = result["orders"][1]
    assert failed["kind"] == "exit" and failed["quantity"] == 200
    assert failed["reason"] == "side_not_executable"
    assert [(trade["strategy"], trade["holding_sessions"]) for trade in result["trades"]] == [
        ("fast", 2),
        ("slow", 3),
    ]
    assert D(result["curve"][2]["cash"]) == D("60000")
    assert D(result["curve"][3]["cash"]) == D("80000")


def test_terminal_unfilled_exits_retain_marked_positions_and_pending_quantity():
    source = flat_source()
    for market in source["market"]["sessions"][2:]:
        market["rules"][0]["can_sell"] = False
    result = run_portfolio_simulation(source)
    assert result["status"] == "complete"
    assert not result["metrics"]["all_positions_flat"]
    assert result["metrics"]["pending_exit_quantity"] == 400
    assert sum(lot["quantity"] for lot in result["positions"]) == 400
    assert D(result["metrics"]["final_cash"]) == D("60000")
    assert D(result["metrics"]["final_equity"]) == D("100000")
    assert result["trades"] == []


def test_loss_gate_stops_new_risk_but_does_not_suppress_due_exit():
    source = flat_source()
    source["limits"]["max_daily_loss_fraction"] = "0.05"
    for index in range(1, len(source["sessions"])):
        reprice(source, index, close="50", opening="50" if index > 1 else None)
    result = run_portfolio_simulation(source)
    assert result["status"] == "complete"
    assert result["plans"][1]["plan"]["status"] == "blocked"
    assert "daily_loss_halt" in result["plans"][1]["plan"]["blocked_reasons"]
    assert (
        result["trades"][0]["strategy"] == "fast" and result["trades"][0]["holding_sessions"] == 1
    )
    assert result["metrics"]["all_positions_flat"]


@pytest.mark.parametrize("phase", ["opening", "decision"])
def test_unknown_held_risk_terms_block_before_using_them_for_margin(phase):
    source = flat_source()
    index = 2 if phase == "opening" else 1
    frame = source["sessions"][index]
    at = (
        source["market"]["sessions"][index]["open_at"]
        if phase == "opening"
        else frame["decision_at"]
    )
    frame[phase + "_instruments"][0]["rule_known_at"] = (
        datetime.fromisoformat(at) + timedelta(seconds=1)
    ).isoformat()
    result = run_portfolio_simulation(source)
    assert result["status"] == "blocked" and result["metrics"] is None
    assert result["reason"] == (
        "held_opening_risk_evidence_unavailable"
        if phase == "opening"
        else "held_decision_evidence_unavailable"
    )
    if phase == "opening":
        assert result["trades"][0]["strategy"] == "fast"  # A valid reducing exit happens first.
        assert [lot["strategy"] for lot in result["positions"]] == ["slow"]


def test_unheld_future_rule_rejects_only_the_candidate():
    source = flat_source()
    at = source["market"]["sessions"][1]["open_at"]
    source["sessions"][1]["opening_instruments"][0]["rule_known_at"] = (
        datetime.fromisoformat(at) + timedelta(seconds=1)
    ).isoformat()
    result = run_portfolio_simulation(source)
    assert result["status"] == "complete" and not result["positions"]
    assert result["orders"][0]["reason"] == "opening_reservation_risk"


def test_unpriced_held_open_retries_its_exit_while_other_priced_exit_executes():
    source = flat_source(two_codes=True)
    source["strategies"][1]["holding_sessions"] = 1
    source["market"]["sessions"][2]["bars"][1]["open"] = None
    result = run_portfolio_simulation(source)
    assert result["status"] == "complete"
    assert [(trade["code"], trade["holding_sessions"]) for trade in result["trades"]] == [
        ("SYN_LONG", 1),
        ("SYN_SHORT", 2),
    ]
    assert result["opening_halts"]
    assert any(event["reason"] == "opening_price_unavailable" for event in result["orders"])


def test_unpriced_held_open_prevents_new_other_code_entry():
    source = flat_source(two_codes=True)
    fast, slow = source["sessions"][0]["signals"]
    source["sessions"][0]["signals"] = [slow]
    source["sessions"][1]["signals"] = [
        fast
        | {
            "decision_at": source["sessions"][1]["decision_at"],
            "known_at": source["sessions"][1]["decision_at"],
            "feature_known_at": source["sessions"][1]["decision_at"],
        }
    ]
    source["market"]["sessions"][2]["bars"][1]["open"] = None
    result = run_portfolio_simulation(source)
    assert result["status"] == "complete"
    assert any(
        event["code"] == "SYN_LONG" and event["reason"] == "held_opening_mark_unavailable"
        for event in result["orders"]
    )
    assert not any(trade["code"] == "SYN_LONG" for trade in result["trades"])


def test_fatal_short_margin_failure_is_explicit_and_not_force_liquidated():
    source = flat_source(-1)
    reprice(source, 1, close="200")
    result = run_portfolio_simulation(source)
    assert result["status"] == "blocked"
    assert result["reason"] == "unsupported_closing_margin_or_equity_breach"
    assert result["metrics"] is None and result["positions"]
    assert not result["trades"]


def test_known_calendar_tail_guard_excludes_only_unfinishable_horizons():
    source = flat_source()
    source["strategies"][1]["holding_sessions"] = 60
    result = run_portfolio_simulation(source)
    assert result["status"] == "complete"
    assert [trade["strategy"] for trade in result["trades"]] == ["fast"]
    assert result["planning_rejections"][0]["strategy"] == "slow"
    assert result["planning_rejections"][0]["reason"] == "insufficient_execution_tail"


def test_nonterminating_fee_and_carry_shares_reconcile_with_declared_precision():
    source = flat_source(-1)
    source["strategies"][0]["weight"] = "0.15"
    for market in source["market"]["sessions"]:
        market["rules"][0].update(minimum_commission="1", borrow_annual_rate="0.01")
    result = run_portfolio_simulation(source)
    assert result["status"] == "complete" and result["metrics"]["accounting_reconciled"]
    assert result["accounting_diagnostics"]["commission_allocations_exact"]
    assert abs(D(result["accounting_diagnostics"]["equity_residual"])) <= D("1e-24")


def test_future_execution_truth_does_not_change_earlier_plans_or_entry_quantity():
    source = flat_source()
    first = run_portfolio_simulation(source)
    reprice(source, 4, opening="500", close="700")
    source["market"]["sessions"][4]["rules"][0]["opening_executable_quantity"] = 0
    second = run_portfolio_simulation(source)
    assert first["plans"][:4] == second["plans"][:4]
    assert first["orders"][0] == second["orders"][0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(mode="historical"),
        lambda s: s["sessions"][1]["opening_instruments"][0].update(mark_price="101"),
        lambda s: s["sessions"][1]["decision_instruments"][0].update(mark_price="101"),
        lambda s: s["sessions"][0]["signals"].append(copy.deepcopy(s["sessions"][0]["signals"][0])),
        lambda s: s["sessions"][0]["opening_correlations"].append(
            {
                "code_a": "SYN_LONG",
                "code_b": "SYN_LONG",
                "correlation": "1",
                "window_end": s["sessions"][0]["decision_at"],
                "known_at": s["sessions"][0]["decision_at"],
                "valid_until": s["sessions"][1]["decision_at"],
            }
        ),
        lambda s: s["market"].update(
            actions=[
                {
                    "kind": "split",
                    "code": "SYN_LONG",
                    "effective_date": s["market"]["sessions"][2]["date"],
                    "known_at": s["market"]["sessions"][2]["open_at"],
                    "ratio": "2",
                }
            ]
        ),
        lambda s: s["sessions"][1]["opening_instruments"][0].update(
            short_additional_collateral_ratio="2"
        ),
    ],
)
def test_inconsistent_or_unsupported_source_is_rejected_before_simulation(mutate):
    source = flat_source()
    mutate(source)
    with pytest.raises(ValidationError):
        run_portfolio_simulation(source)


def test_cli_comparison_is_api_free_and_replays_same_full_input(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.Settings, "load", lambda *_: pytest.fail("Acquisition settings read"))
    monkeypatch.setattr(cli, "api_key", lambda: pytest.fail("API key requested"))
    output, scenario = tmp_path / "result.json", tmp_path / "scenario.json"
    assert cli.main(["portfolio-simulation", "--output", str(output)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["comparison_complete"] and not summary["execution_enabled"]
    first = output.read_bytes()
    scenario.write_text(json.dumps(json.loads(first)["input"]))
    assert (
        cli.main(["portfolio-simulation", "--scenario", str(scenario), "--output", str(output)])
        == 0
    )
    assert output.read_bytes() == first
    capsys.readouterr()
    invalid = flat_source()
    invalid["mode"] = "historical"
    scenario.write_text(json.dumps(invalid))
    assert cli.main(["portfolio-simulation", "--scenario", str(scenario)]) == 2
