import copy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.execution.adapter import intents_from_plan
from trading_system.execution.demo import mock_config
from trading_system.execution.mock import MockGateway
from trading_system.execution.models import MockConfig
from trading_system.execution.service import ExecutionManager
from trading_system.portfolio.benchmark import synthetic_plan
from trading_system.portfolio.planner import plan_portfolio
from trading_system.storage.store import canonical, digest


def source():
    result = plan_portfolio(synthetic_plan())
    prices = {p["code"]: Decimal(p["planning_risk_price"]) for p in result["proposals"]}
    expires = datetime.fromisoformat(result["input"]["decision_at"]) + timedelta(minutes=5)
    return result, prices, expires


def test_verified_plan_quantities_and_source_are_preserved_with_repeatable_ids():
    plan, prices, expires = source()
    before = copy.deepcopy(plan)
    intents = intents_from_plan(plan, limit_prices=prices, expires_at=expires)
    assert len(intents) == 2
    assert intents == intents_from_plan(plan, limit_prices=prices, expires_at=expires)
    assert plan == before
    by_code = {item.code: item for item in intents}
    for proposal in plan["proposals"]:
        intent = by_code[proposal["code"]]
        assert intent.quantity == proposal["quantity"]
        assert intent.source_hash == plan["result_hash"]
        assert intent.side == ("buy" if proposal["direction"] > 0 else "short")


def test_price_change_does_not_create_a_new_identity_for_the_same_proposal():
    plan, prices, expires = source()
    first = intents_from_plan(plan, limit_prices=prices, expires_at=expires)
    prices = {code: price + 1 for code, price in prices.items()}
    changed = intents_from_plan(plan, limit_prices=prices, expires_at=expires)
    assert [item.client_id for item in first] == [item.client_id for item in changed]
    assert first != changed


@pytest.mark.parametrize("resign", [False, True])
def test_modified_proposals_cannot_bypass_reproduction(resign):
    plan, prices, expires = source()
    plan["proposals"][0]["quantity"] += 100
    if resign:
        plan["result_hash"] = digest(
            canonical({k: v for k, v in plan.items() if k != "result_hash"}).encode()
        )
    with pytest.raises(ValueError):
        intents_from_plan(plan, limit_prices=prices, expires_at=expires)


@pytest.mark.parametrize("failure", ["missing", "extra", "nan", "negative", "float", "expired"])
def test_explicit_mock_prices_and_expiry_required(failure):
    plan, prices, expires = source()
    code = next(iter(prices))
    if failure == "missing":
        prices.pop(code)
    elif failure == "extra":
        prices["unknown"] = Decimal(1)
    elif failure == "nan":
        prices[code] = Decimal("NaN")
    elif failure == "negative":
        prices[code] = Decimal(-1)
    elif failure == "float":
        prices[code] = 100.0
    else:
        expires = datetime.fromisoformat(plan["input"]["decision_at"])
    with pytest.raises(ValueError):
        intents_from_plan(plan, limit_prices=prices, expires_at=expires)


def test_planner_to_durable_mock_fills_preserves_identity_and_account(tmp_path):
    plan, prices, expires = source()
    intents = intents_from_plan(plan, limit_prices=prices, expires_at=expires)
    now = intents[0].decision_at
    config = mock_config().model_dump(mode="json")
    template = config["rules"][0]
    config["evidence_at"] = now.isoformat()
    config["rules"] = [
        template
        | {
            "code": code,
            "mark": str(price),
            "lower": "1",
            "upper": "1000",
            "known_at": now.isoformat(),
            "valid_from": now.isoformat(),
            "valid_until": expires.isoformat(),
        }
        for code, price in prices.items()
    ]
    config = MockConfig.model_validate(config)
    gateway = MockGateway(tmp_path / "broker.sqlite", config)
    manager = ExecutionManager(tmp_path / "manager.sqlite", config, gateway)
    try:
        for intent in intents:
            manager.prepare(intent, now)
            manager.dispatch(intent.client_id, now)
            gateway.fill(
                intent.client_id, intent.quantity, intent.limit_price, "fill-" + intent.client_id
            )
            manager.reconcile(intent.client_id, now)
            assert manager.prepare(intent, now)["state"] == "FILLED"
            with pytest.raises(ValueError):
                manager.dispatch(intent.client_id, now)
        assert manager.reconcile_account()
        assert manager.snapshot()["account"] == gateway.account()
        assert gateway.account()["positions"] == {"SYN_A": 200, "SYN_B": -100}
        changed = intents_from_plan(
            plan, limit_prices={k: v + 1 for k, v in prices.items()}, expires_at=expires
        )
        with pytest.raises(ValueError):
            manager.prepare(changed[0], now)
        assert all(order["send_attempt_count"] == 1 for order in gateway.store.orders())
    finally:
        manager.close()
        gateway.close()
