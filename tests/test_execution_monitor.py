"""Read-only, WAL-consistent observations with fail-closed source validation."""

import hashlib
import sqlite3
from datetime import timedelta

import pytest

from trading_system.execution.demo import NOW, mock_config, mock_intent
from trading_system.execution.mock import MockGateway
from trading_system.execution.service import ExecutionManager
from trading_system.monitoring import execution
from trading_system.monitoring.execution import monitor_execution
from trading_system.storage.store import canonical


@pytest.fixture
def source(tmp_path):
    config = mock_config()
    broker = MockGateway(tmp_path / "broker.sqlite", config)
    manager = ExecutionManager(tmp_path / "manager.sqlite", config, broker)
    yield tmp_path / "manager.sqlite", manager, broker
    manager.close()
    broker.close()


def filled(source):
    path, manager, broker = source
    manager.prepare(mock_intent("one"), NOW)
    manager.dispatch("one", NOW)
    broker.fill("one", 100, "100", "fill-one")
    manager.reconcile("one", NOW)
    manager.reconcile_account()
    return path, manager, broker


def codes(result):
    return {alert["code"] for alert in result["alerts"]}


def test_missing_database_does_not_create_directory_or_file(tmp_path):
    path = tmp_path / "missing" / "manager.sqlite"
    with pytest.raises(ValueError, match="^MOCK monitoring source unavailable or invalid$"):
        monitor_execution(path, NOW)
    assert not path.parent.exists()


def test_clean_empty_state_observed_without_operational_or_live_claim(source):
    path, manager, _ = source
    before = manager.store.events()
    result = monitor_execution(path, NOW)
    assert result["status"] == "observed"
    assert result["orders"]["count"] == 0
    assert result["account"]["fixed_marked_synthetic_equity"] == "100000"
    assert result["account"]["aggregate_fill_consistency"]
    assert result["latest_account_reconciliation"] is None
    assert result["operational_readiness"] is False
    assert result["current_broker_connection_verified"] is False
    assert result["trade_permission"] is None
    assert result["account"]["realized_pnl"] is None
    assert result["account"]["unrealized_pnl"] is None
    assert result["account"]["drawdown"] is None
    assert manager.store.events() == before


def test_terminal_state_equity_history_and_determinism_without_mutation(source, monkeypatch):
    path, manager, broker = filled(source)
    before = path.read_bytes()
    broker_before = (path.parent / "broker.sqlite").read_bytes()
    events_before = manager.store.events()

    def forbidden(*args, **kwargs):
        raise AssertionError("Monitor must not initialize a store")

    monkeypatch.setattr("trading_system.execution.store.StateStore.__init__", forbidden)
    first = monitor_execution(path, NOW)
    second = monitor_execution(path, NOW)
    assert first == second
    assert first["status"] == "observed"
    assert first["orders"]["by_state"] == {"FILLED": 1}
    assert first["account"]["fixed_marked_synthetic_equity"] == "99998.00"
    assert first["latest_account_reconciliation"] == {
        "sequence": events_before[-1]["sequence"],
        "matches": True,
        "checked_at": None,
        "historical_only": True,
        "current_proof": False,
    }
    assert first["source"]["fill_count"] == 1
    assert path.read_bytes() == before
    assert (path.parent / "broker.sqlite").read_bytes() == broker_before
    assert manager.store.events() == events_before
    content = {key: value for key, value in first.items() if key != "result_hash"}
    assert first["result_hash"] == hashlib.sha256(canonical(content).encode()).hexdigest()
    assert str(path) not in canonical(first)
    assert broker.account() == manager.store.get_meta("account")


def test_active_wal_committed_changes_are_visible_and_monitor_never_recovers(source):
    path, manager, _ = source
    manager.prepare(mock_intent("one"), NOW)
    with manager.store.transaction():
        order = manager.store.order("one")
        order.update(state="DISPATCHING", send_attempt_count=1)
        manager.store.save_order(order)
    assert path.with_name(path.name + "-wal").exists()
    before = manager.store.events()
    result = monitor_execution(path, NOW)
    assert result["orders"]["by_state"] == {"DISPATCHING": 1}
    assert "order_outcome_uncertain" in codes(result)
    assert "historical_account_reconciliation_missing" in codes(result)
    assert manager.store.order("one")["state"] == "DISPATCHING"
    assert manager.store.events() == before


def test_single_read_transaction_does_not_mix_concurrent_commit(source, monkeypatch):
    path, manager, broker = source
    original_connect = sqlite3.connect
    changed = False
    statements = []

    class Connection:
        def __init__(self, real):
            self.real = real

        def execute(self, sql, *args):
            nonlocal changed
            statements.append(sql)
            cursor = self.real.execute(sql, *args)
            if sql.startswith("SELECT key,value FROM metadata") and not changed:
                changed = True
                manager.prepare(mock_intent("one"), NOW)
                manager.dispatch("one", NOW)
                broker.fill("one", 100, "100", "one-fill")
                manager.reconcile("one", NOW)
            return cursor

        def close(self):
            self.real.close()

    def connect(database, **kwargs):
        assert database.endswith("?mode=ro") and kwargs["uri"] is True
        return Connection(original_connect(database, **kwargs))

    monkeypatch.setattr(execution.sqlite3, "connect", connect)
    result = monitor_execution(path, NOW)
    assert result["orders"]["count"] == 0
    assert result["account"]["cash"] == "100000"
    assert result["source"]["fill_count"] == 0
    assert "BEGIN" in statements and "COMMIT" in statements
    assert monitor_execution(path, NOW)["orders"]["count"] == 1


@pytest.mark.parametrize("state", ["UNKNOWN_SUBMIT", "UNKNOWN_CANCEL", "CANCEL_REQUESTED"])
def test_uncertain_orders_alert_without_state_change(source, state):
    path, manager, _ = source
    manager.prepare(mock_intent("one"), NOW)
    if state != "UNKNOWN_SUBMIT":
        manager.dispatch("one", NOW)
    with manager.store.transaction():
        order = manager.store.order("one")
        order.update(state=state, send_attempt_count=1)
        manager.store.save_order(order)
    result = monitor_execution(path, NOW)
    assert result["status"] == "attention"
    assert "order_outcome_uncertain" in codes(result)
    assert manager.store.order("one")["state"] == state


def test_unknown_submit_after_concurrent_partial_reconcile_is_valid_uncertainty(source):
    path, manager, broker = source
    manager.prepare(mock_intent("one"), NOW)
    manager.dispatch("one", NOW)
    broker.fill("one", 40, "100", "partial")
    manager.reconcile("one", NOW)
    with manager.store.transaction():
        order = manager.store.order("one")
        order["state"] = "UNKNOWN_SUBMIT"
        manager.store.save_order(order)
    result = monitor_execution(path, NOW)
    assert result["orders"]["filled_quantity"] == 40
    assert "order_outcome_uncertain" in codes(result)


def test_expiry_boundary_and_earlier_observation_is_not_historical_state(source):
    path, manager, _ = source
    manager.prepare(mock_intent("one"), NOW)
    before = monitor_execution(path, NOW + timedelta(seconds=3599))
    boundary = monitor_execution(path, NOW + timedelta(hours=1))
    earlier = monitor_execution(path, NOW - timedelta(seconds=1))
    assert "active_order_expired" not in codes(before)
    assert "active_order_expired" in codes(boundary)
    assert "account_evidence_stale" not in codes(boundary)
    assert "account_evidence_future" in codes(earlier)
    assert "order_decision_in_future" in codes(earlier)
    assert earlier["orders"]["count"] == 1
    assert earlier["state_semantics"] == "current_stored_snapshot_not_historical_pit"


@pytest.mark.parametrize("offset,status", [(3601, "stale"), (-1, "future"), (86400, "stale")])
def test_unavailable_evidence_nulls_equity_but_keeps_ledger_visible(source, offset, status):
    path, _, _ = filled(source)
    result = monitor_execution(path, NOW + timedelta(seconds=offset))
    assert result["evidence"]["status"] == status
    assert result["status"] == "attention"
    assert result["account"]["fixed_marked_synthetic_equity"] is None
    assert result["account"]["cash"] == "89998.00"
    assert result["stored_resource_observation"] is None
    if offset == 86400:
        assert "rule_evidence_expired" in codes(result)


@pytest.mark.parametrize("kind", ["emergency", "uncertainty"])
def test_persistent_halts_remain_unchanged(source, kind):
    path, manager, _ = source
    with manager.store.transaction():
        halt = manager.store.get_meta("halt")
        halt[kind] = True
        manager.store.set_meta("halt", halt)
    result = monitor_execution(path, NOW)
    assert kind + "_halt_set" in codes(result)
    assert manager.store.get_meta("halt")[kind] is True


def test_historical_failed_reconcile_and_rejection_are_alerts(source):
    path, manager, _ = source
    manager.prepare(mock_intent("one"), NOW)
    manager.dispatch("one", NOW, reject=True)
    with manager.store.transaction():
        manager.store.event("account_reconciled", {"matches": False})
    result = monitor_execution(path, NOW)
    assert {"historical_order_rejected", "historical_account_reconciliation_failed"} <= codes(
        result
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("cash", "NaN"),
        ("cash", "Infinity"),
        ("cash", 1000),
        ("positions", {"MOCK-A": True}),
        ("positions", {"MOCK-A": 1.5}),
        ("positions", {"UNDECLARED": 10}),
    ],
)
def test_malformed_account_fails_closed_with_generic_error(source, field, value):
    path, manager, _ = source
    with manager.store.transaction():
        account = manager.store.get_meta("account")
        account[field] = value
        manager.store.set_meta("account", account)
    with pytest.raises(ValueError, match="^MOCK monitoring source unavailable or invalid$"):
        monitor_execution(path, NOW)


@pytest.mark.parametrize("patch", [{"cash": "100001"}, {"positions": {"MOCK-A": 10}}])
def test_finite_account_change_cannot_silently_be_observed(source, patch):
    path, manager, _ = source
    with manager.store.transaction():
        account = manager.store.get_meta("account")
        account.update(patch)
        manager.store.set_meta("account", account)
    result = monitor_execution(path, NOW)
    assert "stored_account_fill_mismatch" in codes(result)
    assert result["account"]["aggregate_fill_consistency"] is False
    assert result["account"]["fixed_marked_synthetic_equity"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "UNKNOWN_NEW_STATE"),
        ("filled_quantity", True),
        ("filled_quantity", 101),
        ("send_attempt_count", 2),
        ("broker_id", "unexpected"),
        ("fingerprint", "b" * 64),
        ("client_id", "changed"),
        ("broker_revision", -1),
    ],
)
def test_corrupt_order_refuses_healthy_zero_counts(source, field, value):
    path, manager, _ = source
    manager.prepare(mock_intent("one"), NOW)
    with manager.store.transaction():
        order = manager.store.order("one")
        order[field] = value
        manager.store.db.execute(
            "UPDATE orders SET value=? WHERE client_id='one'", (canonical(order),)
        )
    with pytest.raises(ValueError, match="^MOCK monitoring source unavailable or invalid$"):
        monitor_execution(path, NOW)


@pytest.mark.parametrize(
    "damage",
    [
        "missing_table",
        "missing_meta",
        "invalid_json",
        "duplicate_json",
        "invalid_config",
        "malformed_halt",
        "bad_reconcile",
    ],
)
def test_corrupt_sources_are_sanitized_not_empty_observed(source, damage):
    path, manager, _ = source
    with manager.store.transaction():
        if damage == "missing_table":
            manager.store.db.execute("DROP TABLE fills")
        elif damage == "missing_meta":
            manager.store.db.execute("DELETE FROM metadata WHERE key='account'")
        elif damage in {"invalid_json", "duplicate_json"}:
            value = (
                "{private_source"
                if damage == "invalid_json"
                else '{"cash":"100000","cash":"100001","positions":{}}'
            )
            manager.store.db.execute("UPDATE metadata SET value=? WHERE key='account'", (value,))
        elif damage == "invalid_config":
            config = manager.store.get_meta("config")
            config["mode"] = "live"
            manager.store.set_meta("config", config)
        elif damage == "malformed_halt":
            manager.store.set_meta("halt", {"emergency": "false", "uncertainty": False})
        else:
            manager.store.event("account_reconciled", {"matches": "true"})
    with pytest.raises(ValueError) as caught:
        monitor_execution(path, NOW)
    assert str(caught.value) == "MOCK monitoring source unavailable or invalid"
    assert caught.value.__cause__ is None


def test_naive_observation_fails_without_source_mutation(source):
    path, manager, _ = source
    events = manager.store.events()
    with pytest.raises(ValueError):
        monitor_execution(path, NOW.replace(tzinfo=None))
    assert manager.store.events() == events
