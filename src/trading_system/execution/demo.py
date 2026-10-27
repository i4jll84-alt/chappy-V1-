"""Repeatable persistent mock fault demonstration using fixed synthetic evidence."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_system.execution.mock import MockGateway
from trading_system.execution.models import MockConfig, MockRule, OrderIntent
from trading_system.execution.service import ExecutionManager

NOW = datetime(2026, 1, 5, 1, tzinfo=timezone.utc)


def mock_config():
    return MockConfig(
        initial_cash="100000",
        evidence_at=NOW,
        max_evidence_age_seconds=3600,
        initial_positions={},
        rules=(
            MockRule(
                code="MOCK-A",
                mark="100",
                known_at=NOW,
                valid_from=NOW,
                valid_until=NOW + timedelta(days=1),
                lot_size=10,
                tick_size="1",
                lower="50",
                upper="150",
                can_buy=True,
                can_sell=True,
                short_allowed=True,
                short_capacity=1000,
                additional_collateral="1",
                fee_fixed="1",
                fee_per_share="0.01",
            ),
        ),
        max_order_quantity=1000,
        max_order_value="100000",
        max_position_quantity=1000,
        max_gross_value="100000",
        short_credit_limit="100000",
        cash_floor="1000",
        day_start_equity="100000",
        max_daily_loss_fraction="0.1",
    )


def mock_intent(client_id, side="buy", quantity=100):
    return OrderIntent(
        client_id=client_id,
        code="MOCK-A",
        side=side,
        quantity=quantity,
        limit_price="100",
        decision_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        strategy="fixed_mock",
        source_hash="a" * 64,
    )


def run_mock_demo(root):
    root = Path(root)
    config = mock_config()
    gateway = MockGateway(root / "broker.sqlite", config)
    manager = ExecutionManager(root / "manager.sqlite", config, gateway)
    try:
        if not manager.store.order("demo-lost-ack"):
            manager.prepare(mock_intent("demo-lost-ack"), NOW)
            try:
                manager.dispatch("demo-lost-ack", NOW, lose_ack=True)
            except TimeoutError:
                pass
        # Closing/reopening the manager proves reconstruction from durable state.
        manager.close()
        manager = ExecutionManager(root / "manager.sqlite", config, gateway)
        manager.reconcile("demo-lost-ack", NOW)
        broker_order = gateway.snapshot("demo-lost-ack")
        if broker_order["filled_quantity"] == 0:
            gateway.fill("demo-lost-ack", 100, "100", "demo-fill-1")
        manager.reconcile("demo-lost-ack", NOW)
        manager.clear_halt(NOW)
        if not manager.store.order("demo-partial-cancel"):
            manager.prepare(mock_intent("demo-partial-cancel"), NOW)
            manager.dispatch("demo-partial-cancel", NOW)
            gateway.fill("demo-partial-cancel", 40, "100", "demo-fill-2")
            manager.reconcile("demo-partial-cancel", NOW)
            try:
                manager.cancel("demo-partial-cancel", NOW, lose_ack=True)
            except TimeoutError:
                pass
        manager.reconcile("demo-partial-cancel", NOW)
        manager.clear_halt(NOW)
        if not manager.store.order("demo-fill-wins-cancel"):
            manager.prepare(mock_intent("demo-fill-wins-cancel", "sell", 40), NOW)
            manager.dispatch("demo-fill-wins-cancel", NOW)
            gateway.fill("demo-fill-wins-cancel", 40, "100", "demo-fill-3")
            manager.cancel("demo-fill-wins-cancel", NOW)
        manager.halt("demo_emergency_new_entries_only")
        matches = manager.reconcile_account()
        result = manager.snapshot()
        result.update(
            status="complete" if matches else "blocked",
            broker_orders=gateway.store.orders(),
            broker_account=gateway.account(),
            account_matches=matches,
            historical_market_validity=False,
        )
        # Hash includes the full durable audit and independent broker evidence.
        from trading_system.execution.store import digest

        result.pop("result_hash")
        result["result_hash"] = digest(result)
        return result
    finally:
        manager.close()
        gateway.close()
