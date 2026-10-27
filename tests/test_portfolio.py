import copy
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system import cli
from trading_system.portfolio.benchmark import synthetic_plan
from trading_system.portfolio.planner import plan_portfolio

D = Decimal


def timestamp(request, seconds=0):
    return (datetime.fromisoformat(request["decision_at"]) + timedelta(seconds=seconds)).isoformat()


def single(direction=1):
    data = synthetic_plan().model_dump(mode="json")
    data["strategies"] = [{"strategy": "alpha", "weight": "0.2", "priority": 1}]
    data["signals"] = [data["signals"][0] | {"strategy": "alpha", "direction": direction}]
    data["correlations"] = []
    data["limits"].update(
        max_symbol_fraction="1",
        max_sector_fraction="1",
        max_long_fraction="1",
        max_short_fraction="1",
        max_gross_fraction="2",
        short_credit_limit="1000000",
        min_cash_ratio="0",
        max_stress_loss_fraction="1",
    )
    return data


def position(direction=1, quantity=100, strategy="alpha", code="SYN_A"):
    return {
        "code": code,
        "direction": direction,
        "quantity": quantity,
        "allocations": [{"strategy": strategy, "quantity": quantity}],
    }


def reservation(data, direction=1, quantity=100, code="SYN_A"):
    return position(direction, quantity, code=code) | {
        "order_id": "declared-open-entry",
        "planning_price": "100",
        "fee_buffer": "30",
        "known_at": data["decision_at"],
    }


def details(result):
    return [item for rejection in result["rejections"] for item in rejection.get("details", [])]


def test_deterministic_benchmark_netting_attribution_and_exact_resources():
    source = synthetic_plan().model_dump(mode="json")
    original = copy.deepcopy(source)
    result = plan_portfolio(source)
    assert result == plan_portfolio(source)
    assert source == original
    assert result["status"] == "complete" and not result["execution_enabled"]
    assert len(result["proposals"]) == 2
    first, second = result["proposals"]
    assert (first["code"], first["direction"], first["quantity"]) == ("SYN_A", 1, 200)
    assert (second["code"], second["direction"], second["quantity"]) == ("SYN_B", -1, 100)
    assert first["contributors"][0]["strategy"] == "short_term"
    assert first["contributors"][0]["quantity"] == 200
    assert D(result["after"]["available_cash"]) == D("69950")
    assert D(result["after"]["total_gross"]) == D("40000")
    assert D(result["after"]["stress_loss_estimate"]) == D("4050")
    assert sum(item["quantity"] for item in result["netting"][0]["cancelled"]) == 200
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("direction,expected", [(1, "79970"), (-1, "89970")])
def test_long_and_short_new_risk_resource_charges_do_not_credit_sale_proceeds(direction, expected):
    result = plan_portfolio(single(direction))
    assert result["status"] == "complete"
    assert D(result["after"]["equity"]) == D("100000")
    assert D(result["after"]["account_cash"]) == D("100000")
    assert D(result["after"]["available_cash"]) == D(expected)
    assert result["proposals"][0]["quantity"] == 200


def test_existing_short_sale_cash_and_collateral_hand_anchor():
    source = single(-1)
    source["signals"] = []
    source["positions"] = [position(-1, 200)]
    source["account"]["cash"] = "120000"
    result = plan_portfolio(source)
    assert result["status"] == "complete"
    assert D(result["before"]["equity"]) == D("100000")
    assert D(result["before"]["existing_short_restricted_cash"]) == D("30000")
    assert D(result["before"]["available_cash"]) == D("90000")


@pytest.mark.parametrize("direction,cash_charge", [(1, "20030"), (-1, "10030")])
def test_open_reservations_consume_cash_gross_strategy_and_inventory(direction, cash_charge):
    source = single(direction)
    source["reservations"] = [reservation(source, direction, 200)]
    result = plan_portfolio(source)
    assert result["proposals"] == []
    assert D(result["before"]["reservation_cash_charge"]) == D(cash_charge)
    assert D(result["before"]["strategy_gross"]["alpha"]) == D("20000")
    assert D(result["before"]["total_gross"]) == D("20000")
    if direction == -1:
        assert result["before"]["short_quantities"]["SYN_A"] == 200
    assert result["rejections"][0]["reason"] == "remaining_budget_below_one_lot"


def test_held_and_pending_shorts_exhaust_total_inventory_without_netting():
    source = single(-1)
    source["signals"] = []
    source["strategies"][0]["weight"] = "1"
    source["positions"] = [position(-1, 100)]
    source["reservations"] = [reservation(source, -1, 100)]
    source["account"]["cash"] = "110000"
    source["instruments"][0]["short_total_capacity"] = 199
    result = plan_portfolio(source)
    assert result["status"] == "blocked"
    assert "short_total_inventory:SYN_A" in result["blocked_reasons"]
    assert result["before"]["short_quantities"]["SYN_A"] == 200


@pytest.mark.parametrize("opposite", [False, True])
@pytest.mark.parametrize("kind", ["position", "reservation"])
def test_existing_or_reserved_code_blocks_addition_and_reversal(kind, opposite):
    source = single(-1 if opposite else 1)
    source["strategies"][0]["weight"] = "0.4"
    if kind == "position":
        source["positions"] = [position(1, 100)]
    else:
        source["reservations"] = [reservation(source, 1, 100)]
    result = plan_portfolio(source)
    assert result["proposals"] == []
    expected = (
        "unsupported_existing_reversal" if opposite else "duplicate_existing_or_reserved_code"
    )
    assert any(item["reason"] == expected for item in result["rejections"])
    assert result["before"] == result["after"]


def test_same_side_merge_and_priority_preserving_integer_lot_cancellation():
    source = single()
    a = source["signals"][0]
    source["strategies"] = [
        {"strategy": "first", "weight": "0.3", "priority": 1},
        {"strategy": "second", "weight": "0.2", "priority": 2},
        {"strategy": "third", "weight": "0.3", "priority": 3},
    ]
    source["signals"] = [
        a | {"strategy": "first"},
        a | {"strategy": "second"},
        a | {"strategy": "third", "direction": -1},
    ]
    result = plan_portfolio(source)
    assert result["proposals"][0]["quantity"] == 200
    assert result["proposals"][0]["contributors"][0]["strategy"] == "first"
    assert result["proposals"][0]["contributors"][0]["quantity"] == 200
    source["signals"] = source["signals"][:2]
    merged = plan_portfolio(source)["proposals"][0]
    assert merged["quantity"] == 500 and len(merged["contributors"]) == 2


def test_zero_or_fully_netted_code_requires_no_irrelevant_correlation():
    source = synthetic_plan().model_dump(mode="json")
    source["strategies"][0]["weight"] = "0.1"
    source["correlations"] = []
    result = plan_portfolio(source)
    assert [proposal["code"] for proposal in result["proposals"]] == ["SYN_B"]
    assert any(
        item["reason"] == "opposite_contributions_fully_netted" for item in result["rejections"]
    )
    source = single()
    source["signals"][0]["direction"] = 0
    source["instruments"] = []
    result = plan_portfolio(source)
    assert result["status"] == "complete" and result["proposals"] == []
    assert result["rejections"][0]["reason"] == "zero_direction"


def test_confidence_never_changes_sized_quantities():
    source = single()
    source["signals"][0]["confidence"] = "0.001"
    first = plan_portfolio(source)
    source["signals"][0]["confidence"] = "1"
    second = plan_portfolio(source)
    assert first["proposals"] == second["proposals"]
    assert first["input_hash"] != second["input_hash"]


@pytest.mark.parametrize(
    "limit,value,reason,direction",
    [
        ("max_symbol_fraction", "0.1", "symbol_cap:SYN_A", 1),
        ("max_sector_fraction", "0.1", "sector_cap:sector_a", 1),
        ("max_long_fraction", "0.1", "long_gross_cap", 1),
        ("max_short_fraction", "0.1", "short_gross_cap", -1),
        ("max_gross_fraction", "0.1", "total_gross_cap", 1),
        ("short_credit_limit", "10000", "short_credit_limit", -1),
        ("min_cash_ratio", "0.9", "minimum_cash_ratio", 1),
        ("max_stress_loss_fraction", "0.01", "stress_loss_cap", 1),
    ],
)
def test_each_risk_cap_rejects_whole_code_without_reserving_cash(limit, value, reason, direction):
    source = single(direction)
    source["limits"][limit] = value
    result = plan_portfolio(source)
    assert result["status"] == "complete" and result["proposals"] == []
    assert reason in details(result)
    assert result["before"] == result["after"]


def test_strategy_budget_breach_already_in_book_blocks_all_new_risk():
    source = single()
    source["positions"] = [position(1, 400)]
    source["account"]["cash"] = "60000"
    result = plan_portfolio(source)
    assert result["status"] == "blocked"
    assert "strategy_budget:alpha" in result["blocked_reasons"]
    assert result["proposals"] == []


@pytest.mark.parametrize(
    "reference,reason",
    [("day_start_equity", "daily_loss_halt"), ("reference_equity", "reference_loss_halt")],
)
def test_loss_gate_blocks_new_risk_at_exact_threshold(reference, reason):
    source = single()
    if reference == "day_start_equity":
        source["account"][reference] = "125000"
        source["limits"]["max_daily_loss_fraction"] = "0.2"
    else:
        source["account"][reference] = "125000"
        source["limits"]["max_reference_loss_fraction"] = "0.2"
    result = plan_portfolio(source)
    assert result["status"] == "blocked" and reason in result["blocked_reasons"]


@pytest.mark.parametrize("correlation", ["0.9", "-0.9"])
def test_absolute_pair_correlation_ignores_hedge_benefit(correlation):
    source = synthetic_plan().model_dump(mode="json")
    source["correlations"][0]["correlation"] = correlation
    source["limits"]["correlated_pair_cap"] = "0.3"
    result = plan_portfolio(source)
    assert [item["code"] for item in result["proposals"]] == ["SYN_A"]
    assert "correlated_pair_cap:SYN_A:SYN_B" in details(result)
    assert D(result["after"]["available_cash"]) == D("79970")


@pytest.mark.parametrize("change", ["missing", "future", "future_window", "expired_at_decision"])
def test_missing_or_unavailable_correlation_rejects_candidate(change):
    source = synthetic_plan().model_dump(mode="json")
    if change == "missing":
        source["correlations"] = []
    elif change == "future":
        source["correlations"][0]["known_at"] = timestamp(source, 1)
    elif change == "future_window":
        source["correlations"][0]["window_end"] = timestamp(source, 1)
    else:
        source["correlations"][0]["valid_until"] = source["decision_at"]
    result = plan_portfolio(source)
    assert len(result["proposals"]) == 1
    assert any(item.startswith("correlation_evidence_") for item in details(result))


def test_missing_pair_for_existing_plus_reserved_book_blocks_whole_plan():
    source = single()
    source["signals"] = []
    source["strategies"][0]["weight"] = "1"
    source["positions"] = [position(1, 100)]
    source["reservations"] = [reservation(source, 1, 100, "SYN_B") | {"planning_price": "200"}]
    result = plan_portfolio(source)
    assert result["status"] == "blocked"
    assert "correlation_evidence_missing:SYN_A:SYN_B" in result["blocked_reasons"]


@pytest.mark.parametrize(
    "field,value_kind,reason",
    [
        ("mark_known_at", "future", "mark_evidence_unavailable"),
        ("mark_as_of", "stale", "mark_evidence_stale"),
        ("rule_known_at", "future", "rule_sector_inventory_evidence_unavailable"),
        ("valid_from", "future", "rule_sector_inventory_evidence_unavailable"),
        ("valid_until", "now", "rule_sector_inventory_evidence_unavailable"),
    ],
)
def test_new_candidate_evidence_fails_closed(field, value_kind, reason):
    source = single()
    source["instruments"][0][field] = timestamp(
        source, {"future": 1, "stale": -3601, "now": 0}[value_kind]
    )
    result = plan_portfolio(source)
    assert result["status"] == "complete" and result["proposals"] == []
    assert result["rejections"][0]["reason"] == reason
    source["positions"] = [position(1, 100)]
    source["account"]["cash"] = "90000"
    assert plan_portfolio(source)["status"] == "blocked"


def test_mark_and_account_freshness_boundary_uses_observation_not_refetch_time():
    source = single()
    source["instruments"][0]["mark_as_of"] = timestamp(source, -3600)
    source["account"]["as_of"] = timestamp(source, -3600)
    assert plan_portfolio(source)["proposals"]
    source["account"]["as_of"] = timestamp(source, -3601)
    result = plan_portfolio(source)
    assert result["status"] == "blocked" and "account_evidence_stale" in result["blocked_reasons"]


@pytest.mark.parametrize("kind", ["account", "reservation", "signal"])
def test_future_account_reservation_and_signal_evidence_cannot_be_used(kind):
    source = single()
    if kind == "account":
        source["account"]["known_at"] = timestamp(source, 1)
        assert plan_portfolio(source)["status"] == "blocked"
    elif kind == "reservation":
        source["reservations"] = [reservation(source) | {"known_at": timestamp(source, 1)}]
        assert plan_portfolio(source)["status"] == "blocked"
    else:
        source["signals"][0]["feature_known_at"] = timestamp(source, 1)
        result = plan_portfolio(source)
        assert result["proposals"] == []
        assert result["rejections"][0]["reason"] == "signal_temporal_evidence_unavailable"


def test_understated_prices_use_dated_mark_and_reservation_fee_cannot_be_understated():
    source = single()
    source["instruments"][0]["planning_price"] = "50"
    assert plan_portfolio(source)["proposals"][0]["quantity"] == 200
    source["signals"] = []
    source["reservations"] = [
        reservation(source, 1, 100) | {"planning_price": "50", "fee_buffer": "19"}
    ]
    result = plan_portfolio(source)
    assert result["status"] == "blocked"
    assert "reservation_fee_buffer_insufficient:declared-open-entry" in result["blocked_reasons"]
    source["reservations"][0]["fee_buffer"] = "20"
    assert D(plan_portfolio(source)["before"]["reservation_cash_charge"]) == D("10020")


def test_short_permission_inventory_and_more_than_100_percent_stress():
    source = single(-1)
    source["instruments"][0]["short_allowed"] = False
    assert plan_portfolio(source)["rejections"][0]["reason"] == "new_short_not_allowed"
    source["instruments"][0]["short_allowed"] = True
    source["instruments"][0]["short_total_capacity"] = 199
    assert "short_total_inventory:SYN_A" in details(plan_portfolio(source))
    source["instruments"][0]["short_total_capacity"] = 10000
    source["instruments"][0]["adverse_move_fraction"] = "6"
    assert "stress_loss_cap" in details(plan_portfolio(source))


def test_rejected_first_candidate_does_not_consume_resources_for_later_valid_code():
    source = synthetic_plan().model_dump(mode="json")
    source["instruments"][0]["adverse_move_fraction"] = "6"
    result = plan_portfolio(source)
    assert [proposal["code"] for proposal in result["proposals"]] == ["SYN_B"]
    assert D(result["after"]["proposal_cash_charge"]) == D("10020")
    assert D(result["after"]["available_cash"]) == D("89980")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(mode="historical"),
        lambda p: p["signals"].append(copy.deepcopy(p["signals"][0])),
        lambda p: p["strategies"][0].update(weight="1.1"),
        lambda p: p["instruments"][0].update(lot_size=True),
        lambda p: p["instruments"][0].pop("sector"),
        lambda p: p["instruments"][0].update(mark_price="NaN"),
        lambda p: p["instruments"][0].update(short_allowed=1),
        lambda p: p.update(
            positions=[position(1, 100) | {"allocations": [{"strategy": "alpha", "quantity": 99}]}]
        ),
    ],
)
def test_unknown_or_inconsistent_inputs_are_rejected(mutate):
    source = single()
    mutate(source)
    with pytest.raises(ValidationError):
        plan_portfolio(source)


def test_cli_benchmark_is_api_free_and_custom_request_is_repeatable(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.Settings, "load", lambda *_: pytest.fail("Acquisition settings loaded"))
    monkeypatch.setattr(cli, "api_key", lambda: pytest.fail("API key requested"))
    plan, output = tmp_path / "plan.json", tmp_path / "result.json"
    assert cli.main(["portfolio-benchmark", "--output", str(output)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["proposal_count"] == 2 and not summary["execution_enabled"]
    first = json.loads(output.read_bytes())
    plan.write_text(json.dumps(first["input"]))
    assert cli.main(["portfolio-benchmark", "--plan", str(plan), "--output", str(output)]) == 0
    assert json.loads(output.read_bytes()) == first
    capsys.readouterr()
    blocked = single()
    blocked["account"]["known_at"] = timestamp(blocked, 1)
    plan.write_text(json.dumps(blocked))
    assert cli.main(["portfolio-benchmark", "--plan", str(plan)]) == 3
