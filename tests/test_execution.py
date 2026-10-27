"""Fault, restart, resource and cumulative-fill invariants for offline MOCK orders."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.execution.demo import NOW, mock_config, mock_intent, run_mock_demo
from trading_system.execution.mock import MockGateway
from trading_system.execution.models import MockConfig, OrderIntent
from trading_system.execution.service import ExecutionManager


@pytest.fixture
def system(tmp_path):
    config = mock_config()
    broker = MockGateway(tmp_path / "broker.sqlite", config)
    manager = ExecutionManager(tmp_path / "manager.sqlite", config, broker)
    yield manager, broker, config, tmp_path
    manager.close()
    broker.close()


def setup_order(manager, key="one", side="buy", quantity=100):
    intent = mock_intent(key, side, quantity)
    manager.prepare(intent, NOW)
    manager.dispatch(key, NOW)
    return intent


def test_lost_ack_keeps_resources_restart_and_query_never_resends(system):
    manager, broker, config, root = system
    manager.prepare(mock_intent("one"), NOW)
    with pytest.raises(TimeoutError):
        manager.dispatch("one", NOW, lose_ack=True)
    assert manager.store.order("one")["state"] == "UNKNOWN_SUBMIT"
    assert manager.snapshot()["halt"]["uncertainty"]
    second = ExecutionManager(root / "manager.sqlite", config, broker)
    try:
        with pytest.raises(ValueError):
            second.dispatch("one", NOW)
        with pytest.raises(ValueError):
            second.prepare(mock_intent("two"), NOW)
        second.reconcile("one", NOW)
        second.clear_halt(NOW)
        assert broker.snapshot("one")["send_attempt_count"] == 1
        assert second.store.order("one")["state"] == "ACKNOWLEDGED"
    finally:
        second.close()


def test_process_crash_dispatching_recovered_unknown_no_gateway_call(system):
    manager, broker, config, root = system
    manager.prepare(mock_intent("one"), NOW)
    with manager.store.transaction():
        order = manager.store.order("one")
        order.update(state="DISPATCHING", send_attempt_count=1)
        manager.store.save_order(order)
    second = ExecutionManager(root / "manager.sqlite", config, broker)
    try:
        assert second.store.order("one")["state"] == "UNKNOWN_SUBMIT"
        second.reconcile("one", NOW)
        with pytest.raises(ValueError):
            second.clear_halt(NOW)
        assert broker.store.orders() == []
    finally:
        second.close()


def test_same_key_payload_idempotent_changed_payload_refused_and_audited(system):
    manager, broker, _, _ = system
    intent = mock_intent("one")
    first = manager.prepare(intent, NOW)
    assert manager.prepare(intent, NOW) == first
    with pytest.raises(ValueError):
        manager.prepare(intent.model_copy(update={"quantity": 110}), NOW)
    assert manager.store.order("one") == first
    assert manager.store.events()[-1]["kind"] == "request_failed"
    assert broker.store.orders() == []


def test_concurrent_managers_cannot_dispatch_same_intent(system):
    manager, broker, config, root = system
    manager.prepare(mock_intent("one"), NOW)
    other = ExecutionManager(root / "manager.sqlite", config, broker)
    try:
        manager.dispatch("one", NOW)
        with pytest.raises(ValueError):
            other.dispatch("one", NOW)
        assert broker.snapshot("one")["send_attempt_count"] == 1
    finally:
        other.close()


def test_expired_never_sent_local_cancel_releases_without_broker(system):
    manager, broker, _, _ = system
    manager.prepare(mock_intent("one", quantity=900), NOW)
    with pytest.raises(ValueError):
        manager.dispatch("one", NOW + timedelta(hours=1))
    manager.cancel("one", NOW + timedelta(hours=1))
    assert manager.store.order("one")["send_attempt_count"] == 0
    manager.prepare(mock_intent("two", quantity=900), NOW)
    assert broker.store.orders() == []
    assert manager.reconcile_account()


def test_partial_cancel_unknown_retains_fill_and_final_releases_only_remainder(system):
    manager, broker, _, _ = system
    setup_order(manager)
    broker.fill("one", 40, "99", "f1")
    manager.reconcile("one", NOW)
    before = manager.store.get_meta("account")
    with pytest.raises(TimeoutError):
        manager.cancel("one", NOW, lose_ack=True)
    assert manager.store.order("one")["state"] == "UNKNOWN_CANCEL"
    assert manager.store.get_meta("account") == before
    manager.reconcile("one", NOW)
    assert manager.store.order("one")["state"] == "CANCELLED"
    assert manager.store.order("one")["filled_quantity"] == 40
    assert Decimal(before["cash"]) == Decimal("96038.60")
    assert before["positions"] == {"MOCK-A": 40}
    assert manager.reconcile_account()
    manager.clear_halt(NOW)


def test_fill_wins_cancel_and_duplicate_snapshot_exact_once(system):
    manager, broker, _, _ = system
    setup_order(manager)
    stale = broker.snapshot("one")
    broker.fill("one", 40, "100", "f1")
    broker.fill("one", 60, "100", "f2")
    manager.cancel("one", NOW)
    account = manager.store.get_meta("account")
    manager.reconcile("one", NOW)
    manager.ingest(stale, NOW)
    assert manager.store.order("one")["state"] == "FILLED"
    assert manager.store.get_meta("account") == account
    assert Decimal(account["cash"]) == Decimal("89998")
    assert len([e for e in manager.store.events() if e["kind"] == "fill_applied"]) == 2


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x.update(filled_quantity=True),
        lambda x: x.update(broker_id=""),
        lambda x: x.update(filled_quantity=110),
        lambda x: x["fills"][0].update(fill_id=""),
        lambda x: x["fills"][0].update(price="101"),
        lambda x: x["fills"][0].update(fee="0"),
        lambda x: x["fills"][0].update(client_id="impostor"),
        lambda x: x["fills"][0].update(quantity=True),
        lambda x: x["fills"].append(deepcopy(x["fills"][0])),
    ],
)
def test_malformed_snapshot_rolls_back_entire_ledger_and_halts(system, change):
    manager, broker, _, _ = system
    setup_order(manager)
    broker.fill("one", 40, "100", "f1")
    snapshot = broker.snapshot("one")
    change(snapshot)
    before = manager.store.get_meta("account")
    with pytest.raises((ValueError, TypeError)):
        manager.ingest(snapshot, NOW)
    assert manager.store.get_meta("account") == before
    assert manager.store.order("one")["filled_quantity"] == 0
    assert manager.snapshot()["halt"]["uncertainty"]


def test_contradictory_same_revision_and_terminal_extra_fill_halt(system):
    manager, broker, _, _ = system
    setup_order(manager)
    bad = broker.snapshot("one")
    bad["broker_id"] = "OTHER"
    with pytest.raises(ValueError):
        manager.ingest(bad, NOW)
    manager.reconcile("one", NOW)
    manager.clear_halt(NOW)
    manager.cancel("one", NOW)
    bad = broker.snapshot("one")
    bad.update(revision=bad["revision"] + 1, filled_quantity=10)
    bad["fills"] = [{"fill_id": "phantom", "quantity": 10, "price": "100", "fee": "1.1"}]
    with pytest.raises(ValueError):
        manager.ingest(bad, NOW)
    assert manager.store.order("one")["filled_quantity"] == 0


@pytest.mark.parametrize("method", ["snapshot", "account", "order_inventory"])
def test_query_failures_persist_halt_and_do_not_release_order(system, monkeypatch, method):
    manager, broker, _, _ = system
    setup_order(manager)

    def fail(*args):
        raise TimeoutError("offline fault")

    monkeypatch.setattr(broker, method, fail)
    with pytest.raises(TimeoutError):
        manager.reconcile("one", NOW) if method == "snapshot" else manager.clear_halt(NOW)
    assert manager.snapshot()["halt"]["uncertainty"]
    assert manager.store.order("one")["state"] == "ACKNOWLEDGED"


def test_unsolicited_unfilled_broker_order_blocks_account_clear(system):
    manager, broker, _, _ = system
    broker.submit(mock_intent("unsolicited"))
    assert manager.store.get_meta("account") == broker.account()
    assert not manager.reconcile_account()
    with pytest.raises(ValueError):
        manager.clear_halt(NOW)
    assert manager.snapshot()["halt"]["uncertainty"]


def test_new_dispatch_requires_fresh_broker_inventory_and_accounts(system):
    manager, broker, _, _ = system
    setup_order(manager)
    manager.prepare(mock_intent("two"), NOW)
    broker.fill("one", 100, "100", "unseen")
    with pytest.raises(ValueError):
        manager.dispatch("two", NOW)
    assert broker.snapshot("two") is None
    assert manager.store.order("two")["send_attempt_count"] == 0
    manager.reconcile("one", NOW)
    manager.clear_halt(NOW)
    manager.dispatch("two", NOW)
    assert broker.snapshot("two")["send_attempt_count"] == 1


def test_emergency_new_entry_only_safe_sell_still_allowed(system):
    manager, broker, _, _ = system
    setup_order(manager)
    broker.fill("one", 100, "100", "f1")
    manager.reconcile("one", NOW)
    manager.halt("operator")
    with pytest.raises(ValueError):
        manager.prepare(mock_intent("new"), NOW)
    setup_order(manager, "exit", "sell", 100)
    broker.fill("exit", 100, "100", "f2")
    manager.reconcile("exit", NOW)
    assert manager.store.get_meta("account")["positions"]["MOCK-A"] == 0
    assert manager.snapshot()["halt"]["emergency"]


@pytest.mark.parametrize("side", ["sell", "cover"])
def test_closing_quantity_cannot_be_reserved_twice(tmp_path, side):
    config = mock_config().model_copy(
        update={"initial_positions": {"MOCK-A": 100 if side == "sell" else -100}}
    )
    broker = MockGateway(tmp_path / "b", config)
    manager = ExecutionManager(tmp_path / "m", config, broker)
    try:
        manager.prepare(mock_intent("one", side, 60), NOW)
        with pytest.raises(ValueError):
            manager.prepare(mock_intent("two", side, 50), NOW)
        assert len(manager.store.orders()) == 1
    finally:
        manager.close()
        broker.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"max_order_quantity": 90},
        {"max_order_value": Decimal("9999")},
        {"max_position_quantity": 90},
        {"max_gross_value": Decimal("9999")},
        {"cash_floor": Decimal("90000")},
        {"day_start_equity": Decimal("120000")},
    ],
)
def test_buy_risk_caps_refuse_without_reservation(tmp_path, patch):
    config = mock_config().model_copy(update=patch)
    broker = MockGateway(tmp_path / "b", config)
    manager = ExecutionManager(tmp_path / "m", config, broker)
    try:
        with pytest.raises(ValueError):
            manager.prepare(mock_intent("one"), NOW)
        assert manager.store.orders() == []
        assert manager.store.events()[-1]["kind"] == "request_failed"
    finally:
        manager.close()
        broker.close()


def test_short_gap_reservation_cash_collateral_and_cover(tmp_path):
    config = mock_config().model_copy(update={"cash_floor": Decimal("89950")})
    broker = MockGateway(tmp_path / "b", config)
    manager = ExecutionManager(tmp_path / "m", config, broker)
    try:
        bad = mock_intent("bad", "short").model_copy(update={"limit_price": Decimal("99")})
        with pytest.raises(ValueError):
            manager.prepare(bad, NOW)
        setup_order(manager, "short", "short")
        broker.fill("short", 100, "100", "f1")
        manager.reconcile("short", NOW)
        assert Decimal(manager.store.get_meta("account")["cash"]) == Decimal("109998")
        setup_order(manager, "cover", "cover")
        broker.fill("cover", 100, "100", "f2")
        manager.reconcile("cover", NOW)
        assert Decimal(manager.store.get_meta("account")["cash"]) == Decimal("99996")
        assert manager.reconcile_account()
    finally:
        manager.close()
        broker.close()


@pytest.mark.parametrize(
    "kind",
    [
        "stale",
        "future_rule",
        "expired_rule",
        "lot",
        "tick",
        "band",
        "short_permission",
        "inventory",
        "credit",
    ],
)
def test_explicit_rule_and_short_boundaries(tmp_path, kind):
    payload = mock_config().model_dump(mode="json")
    rule = payload["rules"][0]
    side, quantity, limit = "buy", 100, "100"
    if kind == "stale":
        payload["max_evidence_age_seconds"] = 0
    elif kind == "future_rule":
        rule["known_at"] = NOW + timedelta(seconds=2)
    elif kind == "expired_rule":
        rule["valid_until"] = NOW + timedelta(seconds=1)
    elif kind == "lot":
        quantity = 101
    elif kind == "tick":
        limit = "100.5"
    elif kind == "band":
        limit = "151"
    elif kind == "short_permission":
        side, rule["short_allowed"] = "short", False
    elif kind == "inventory":
        side, rule["short_capacity"] = "short", 90
    elif kind == "credit":
        side, payload["short_credit_limit"] = "short", "9999"
    config = MockConfig.model_validate(payload)
    broker = MockGateway(tmp_path / "b", config)
    manager = ExecutionManager(tmp_path / "m", config, broker)
    try:
        intent = mock_intent("one", side, quantity).model_copy(
            update={"limit_price": Decimal(limit)}
        )
        with pytest.raises(ValueError):
            manager.prepare(intent, NOW + timedelta(seconds=1))
    finally:
        manager.close()
        broker.close()


def test_config_reopen_never_resets_cash_and_changed_config_refused(system):
    manager, broker, config, root = system
    setup_order(manager)
    broker.fill("one", 100, "100", "f1")
    manager.reconcile("one", NOW)
    before = manager.store.get_meta("account")
    reopened = ExecutionManager(root / "manager.sqlite", config, broker)
    assert reopened.store.get_meta("account") == before
    reopened.close()
    with pytest.raises(ValueError):
        ExecutionManager(
            root / "manager.sqlite",
            config.model_copy(update={"initial_cash": Decimal("200000")}),
            broker,
        )


@pytest.mark.parametrize("value", [True, 1.5, "10"])
def test_initial_positions_reject_coercion(value):
    payload = mock_config().model_dump(mode="json")
    payload["initial_positions"] = {"MOCK-A": value}
    with pytest.raises(ValueError):
        MockConfig.model_validate(payload)


def test_demo_rerun_preserves_orders_fills_account_and_audit_prefix(tmp_path):
    first = run_mock_demo(tmp_path)
    second = run_mock_demo(tmp_path)
    for key in ("orders", "broker_orders", "account", "broker_account"):
        assert first[key] == second[key]
    assert second["events"][: len(first["events"])] == first["events"]
    assert all(o["send_attempt_count"] == 1 for o in second["broker_orders"])
    assert first["account_matches"]
    assert {o["state"] for o in first["orders"]} == {"FILLED", "CANCELLED"}


def test_intent_requires_aware_times_and_strict_quantity():
    payload = mock_intent("one").model_dump(mode="json")
    payload["quantity"] = True
    with pytest.raises(ValueError):
        OrderIntent.model_validate(payload)
