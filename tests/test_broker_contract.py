"""Contract, lifecycle, routing and fail-closed tests: no sockets or Excel."""

import socket
from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.brokers.__main__ import main
from trading_system.brokers.base import BrokerAdapter, Unavailable
from trading_system.brokers.capabilities import Feature
from trading_system.brokers.demo import fixture, request, run_demo
from trading_system.brokers.models import Account, CostValue, OrderRequest, PaperMarket
from trading_system.brokers.paper.adapter import Adapter as PaperAdapter
from trading_system.brokers.registry import (
    BROKERS,
    BrokerSetting,
    Config,
    PaperFixture,
    Trading,
    vendor_adapter,
)
from trading_system.brokers.router import BrokerRouter
from trading_system.execution.demo import NOW


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Phase 1 attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def paper(tmp_path):
    f = fixture()
    a = PaperAdapter(tmp_path, Account(broker="rakuten", account_id="test"), f.risk, f.market)
    a.connect()
    yield a
    a.close()


def all_enabled():
    return Config(brokers={b: BrokerSetting(enabled=True) for b in BROKERS})


@pytest.mark.parametrize("broker", BROKERS)
@pytest.mark.parametrize("enabled", [True, False])
def test_vendor_contract_has_no_transport_and_never_fakes_success(broker, enabled):
    account = Account(broker=broker, account_id="test")
    adapter = vendor_adapter(account, enabled=enabled)
    assert isinstance(adapter, BrokerAdapter)
    assert set(adapter.capabilities()) == set(Feature)
    assert adapter.connection_status().live_verified is False
    assert adapter.connection_status().state == ("not_implemented" if enabled else "disabled")
    calls = [
        lambda: adapter.connect(),
        lambda: adapter.quote("MOCK-A", NOW),
        lambda: adapter.board("MOCK-A", NOW),
        lambda: adapter.submit(request(account, "one"), NOW),
        lambda: adapter.order("one", NOW),
        lambda: adapter.orders(NOW),
        lambda: adapter.cancel("one", NOW),
        lambda: adapter.modify("one", Decimal(99), 100, NOW),
        lambda: adapter.fills(NOW),
        lambda: adapter.positions(NOW),
        lambda: adapter.balance(NOW),
        lambda: adapter.short_availability("MOCK-A", "general", NOW),
        lambda: adapter.costs("MOCK-A", NOW),
    ]
    for call in calls:
        with pytest.raises(Unavailable) as error:
            call()
        assert error.value.reason == ("not_implemented" if enabled else "disabled")
    adapter.close()


@pytest.mark.parametrize("broker", BROKERS)
def test_each_broker_selected_as_paper_without_credentials(tmp_path, broker):
    config = all_enabled().model_copy(update={"trading": Trading(broker=broker)})
    router = BrokerRouter(tmp_path, config, fixture())
    try:
        result = run_demo(router)
        assert result["accounting_matches"] is True
        assert result["real_execution_enabled"] is False
        assert result["positions"] == []
        assert Decimal(result["balance"]["cash"]) == Decimal("99996")
        assert len(router.adapter.fills(NOW)) == 4
        assert run_demo(router)["balance"] == result["balance"]
    finally:
        router.close()


def test_partial_fills_and_cancel_keep_independent_accounting(paper):
    order = paper.submit(request(paper.account, "one"), NOW)
    assert order.state == "ACKNOWLEDGED" and order.fills == ()
    assert paper.balance(NOW).cash == 100000
    order = paper.simulate_fill("one", 40, "100", "f1", NOW)
    assert order.state == "PARTIALLY_FILLED" and order.filled_quantity == 40
    assert paper.cancel("one", NOW).state == "CANCELLED"
    assert paper.positions(NOW)[0].quantity == 40
    assert paper.manager.reconcile_account()
    assert paper.submit(request(paper.account, "one"), NOW).state == "CANCELLED"
    assert paper.gateway.snapshot("one")["send_attempt_count"] == 1


@pytest.mark.parametrize("credit", ["general", "standard"])
def test_short_cover_explicit_credit_and_position(paper, credit):
    paper.submit(request(paper.account, "short", "margin_short", credit=credit), NOW)
    paper.simulate_fill("short", 100, "100", "f1", NOW)
    position = paper.positions(NOW)[0]
    assert position.side == "short" and position.credit == credit
    paper.submit(
        request(
            paper.account, "cover", "margin_cover", credit=credit, position_id=position.position_id
        ),
        NOW,
    )
    paper.simulate_fill("cover", 100, "100", "f2", NOW)
    assert paper.positions(NOW) == ()
    assert paper.balance(NOW).cash == 99996


def test_unknown_inventory_and_wrong_credit_are_refused(paper):
    f = fixture().market.model_dump(mode="json")
    f["shorts"] = []
    paper.market = PaperMarket.model_validate(f)
    with pytest.raises(Unavailable):
        paper.submit(request(paper.account, "one", "margin_short", credit="general"), NOW)
    assert paper.gateway.order_inventory() == []


def test_credit_inventory_reserves_pending_orders(paper):
    paper.submit(request(paper.account, "one", "margin_short", 600, credit="general"), NOW)
    with pytest.raises(ValueError, match="short_inventory"):
        paper.submit(request(paper.account, "two", "margin_short", 500, credit="general"), NOW)
    with pytest.raises(ValueError, match="credit_types"):
        paper.submit(request(paper.account, "three", "margin_short", 100, credit="standard"), NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "margin_buy"},
        {"action": "margin_sell"},
        {"action": "margin_short"},
        {"credit": "general"},
        {"quantity": True},
        {"limit_price": "NaN"},
        {"action": "margin_cover", "credit": "general"},
    ],
)
def test_unsupported_trade_types_and_invalid_values(paper, changes):
    value = request(paper.account, "one").model_dump(mode="json") | changes
    with pytest.raises(ValueError):
        OrderRequest.model_validate(value)


@pytest.mark.parametrize("mode,live", [("live", False), ("paper", True), ("paper", "false")])
def test_config_cannot_enable_real_trading(mode, live):
    with pytest.raises(ValueError):
        Trading(mode=mode, live_trading_enabled=live)


def test_disabled_selected_and_unknown_config_rejected():
    with pytest.raises(ValueError):
        Config(trading=Trading(broker="tachibana"))
    with pytest.raises(ValueError):
        Config.model_validate({"api_key": "secret"})
    with pytest.raises(ValueError):
        Config(brokers={})
    assert Config().brokers["tachibana"].enabled is False


def test_restart_and_changed_route_payload_never_resend(tmp_path):
    a = Account(broker="rakuten", account_id="test")
    f = fixture()
    p = PaperAdapter(tmp_path, a, f.risk, f.market)
    p.connect()
    p.submit(request(a, "one"), NOW)
    p.close()
    p = PaperAdapter(tmp_path, a, f.risk, f.market)
    try:
        p.connect()
        assert p.submit(request(a, "one"), NOW).state == "ACKNOWLEDGED"
        changed = request(a, "one").model_copy(update={"limit_price": Decimal(99)})
        with pytest.raises(ValueError, match="immutable"):
            p.submit(changed, NOW)
        assert p.gateway.snapshot("one")["send_attempt_count"] == 1
    finally:
        p.close()


def test_lost_ack_queries_after_restart_no_retry(tmp_path, monkeypatch):
    config = Config()
    router = BrokerRouter(tmp_path, config, fixture())
    original = router.adapter.gateway.submit
    monkeypatch.setattr(
        router.adapter.gateway, "submit", lambda intent: original(intent, lose_ack=True)
    )
    order = request(router.active, "one")
    with pytest.raises(TimeoutError):
        router.submit(order, NOW)
    assert router.adapter.manager.store.order("one")["state"] == "UNKNOWN_SUBMIT"
    router.close()
    router = BrokerRouter(tmp_path, config, fixture())
    try:
        assert router.submit(order, NOW).state == "ACKNOWLEDGED"
        assert router.adapter.gateway.snapshot("one")["send_attempt_count"] == 1
        with pytest.raises(ValueError):
            router.submit(request(router.active, "two"), NOW)
    finally:
        router.close()


def test_unknown_route_claim_without_ack_is_not_retried(tmp_path):
    from trading_system.execution.store import digest
    from trading_system.storage.store import canonical

    r = BrokerRouter(tmp_path, Config(), fixture())
    try:
        order = request(r.active, "one")
        with r.db:
            r.db.execute(
                "INSERT INTO routes VALUES(?,?,?)",
                (
                    "one",
                    digest(order.model_dump(mode="json")),
                    canonical(order.account.model_dump(mode="json")),
                ),
            )
        with pytest.raises(Unavailable, match="unknown"):
            r.submit(order, NOW)
        assert r.adapter.gateway.order_inventory() == []
    finally:
        r.close()


def test_switch_blocks_pending_then_held_then_succeeds_and_pins_routes(tmp_path):
    r = BrokerRouter(tmp_path, all_enabled(), fixture())
    try:
        old = r.active
        req = request(old, "one")
        r.submit(req, NOW)
        with pytest.raises(ValueError, match="open_or_unknown"):
            r.switch("tachibana", NOW)
        r.adapter.simulate_fill("one", 100, "100", "f1", NOW)
        with pytest.raises(ValueError, match="flat"):
            r.switch("tachibana", NOW)
        r.submit(request(old, "sell", "cash_sell"), NOW)
        r.adapter.simulate_fill("sell", 100, "100", "f2", NOW)
        r.switch("tachibana", NOW)
        assert r.adapter.balance(NOW).cash == 100000
        assert r.submit(req, NOW).state == "FILLED"  # Query original account, never reroute.
        with pytest.raises(ValueError, match="immutable_order_route"):
            r.submit(request(r.active, "one"), NOW)
        assert r.adapter.orders(NOW) == ()
        assert r._adapter(old).balance(NOW).cash == 99996
    finally:
        r.close()


def test_disabled_broker_not_instantiated(tmp_path, monkeypatch):
    import trading_system.brokers.router as module

    real = module.build_paper
    created = []

    def build(root, account, fixture):
        created.append(account.broker)
        return real(root, account, fixture)

    monkeypatch.setattr(module, "build_paper", build)
    r = BrokerRouter(tmp_path, Config(), fixture())
    try:
        with pytest.raises(Unavailable, match="disabled"):
            r.switch("tachibana", NOW)
        assert created == ["rakuten"]
    finally:
        r.close()


def test_persisted_route_change_requires_checked_switch(tmp_path):
    r = BrokerRouter(tmp_path, all_enabled(), fixture())
    r.switch("tachibana", NOW)
    r.close()
    with pytest.raises(ValueError, match="explicit_switch"):
        BrokerRouter(tmp_path, all_enabled(), fixture())
    config = all_enabled().model_copy(update={"trading": Trading(broker="tachibana")})
    r = BrokerRouter(tmp_path, config, fixture())
    r.close()


def test_single_writer_prevents_parallel_managers(tmp_path):
    r = BrokerRouter(tmp_path, Config(), fixture())
    with pytest.raises(ValueError, match="already_open"):
        BrokerRouter(tmp_path, Config(), fixture())
    r.close()
    r = BrokerRouter(tmp_path, Config(), fixture())
    r.close()


def test_emergency_stop_persists_no_automatic_cancel_or_liquidation(tmp_path):
    r = BrokerRouter(tmp_path, all_enabled(), fixture())
    r.submit(request(r.active, "one"), NOW)
    r.emergency_stop()
    assert r.adapter.order("one", NOW).state == "ACKNOWLEDGED"
    r.close()
    r = BrokerRouter(tmp_path, all_enabled(), fixture())
    try:
        with pytest.raises(ValueError):
            r.submit(request(r.active, "two"), NOW)
        assert r.adapter.order("one", NOW).state == "ACKNOWLEDGED"
    finally:
        r.close()


def test_disconnect_halts_until_explicit_reconciliation(paper):
    paper.disconnect()
    with pytest.raises(Unavailable):
        paper.submit(request(paper.account, "one"), NOW)
    paper.connect()
    with pytest.raises(ValueError, match="halted"):
        paper.submit(request(paper.account, "one"), NOW)
    paper.resume_entries(NOW)
    assert paper.submit(request(paper.account, "one"), NOW).state == "ACKNOWLEDGED"


@pytest.mark.parametrize("minutes", [-1, 60, 1440])
def test_closed_unknown_sessions_no_send(paper, minutes):
    now = NOW + timedelta(minutes=minutes)
    with pytest.raises(ValueError):
        paper.submit(request(paper.account, "one"), now)
    assert paper.gateway.order_inventory() == []


@pytest.mark.parametrize(
    "quantity,action", [(5, "cash_buy"), (2000, "cash_buy"), (100, "cash_sell")]
)
def test_risk_lot_limit_and_inventory_enforced(paper, quantity, action):
    with pytest.raises(ValueError):
        paper.submit(request(paper.account, "one", action, quantity), NOW)
    assert paper.gateway.order_inventory() == []


def test_query_evidence_and_costs_do_not_invent_data(paper):
    assert paper.quote("MOCK-A", NOW).freshness_verified is False
    assert paper.board("MOCK-A", NOW).evidence == "simulated"
    costs = paper.costs("MOCK-A", NOW)
    assert costs.borrow_annual_rate.value is None
    assert costs.commission_fixed.evidence == "estimated"
    with pytest.raises(ValueError):
        CostValue(evidence="observed", value=None, unit="JPY")
    with pytest.raises(Unavailable):
        paper.quote("MOCK-A", NOW + timedelta(hours=2))
    with pytest.raises(Unavailable):
        paper.board("missing", NOW)
    with pytest.raises(Unavailable, match="unsupported"):
        paper.modify("missing", Decimal(99), 100, NOW)


def test_read_only_doctor_and_repeatable_demo_cli(tmp_path, capsys):
    assert main(["doctor"]) == 0
    assert '"network_connections_attempted": 0' in capsys.readouterr().out
    for _ in range(2):
        assert main(["demo", "--state-root", str(tmp_path)]) == 0
        assert '"accounting_matches": true' in capsys.readouterr().out


def test_account_binding_cannot_be_reused_for_other_broker(tmp_path):
    f = fixture()
    p = PaperAdapter(tmp_path, Account(broker="rakuten", account_id="test"), f.risk, f.market)
    p.close()
    with pytest.raises(ValueError, match="binding"):
        PaperAdapter(tmp_path, Account(broker="tachibana", account_id="test"), f.risk, f.market)


def test_corrupt_venue_account_blocks_new_orders(paper):
    with paper.gateway.store.transaction():
        paper.gateway.store.set_meta("account", {"cash": "1", "positions": {}})
    with pytest.raises(ValueError):
        paper.submit(request(paper.account, "one"), NOW)
    assert paper.gateway.order_inventory() == []


def test_future_short_evidence_and_mismatched_cover_id(paper):
    value = fixture().model_dump(mode="json")
    value["market"]["shorts"][0]["known_at"] = (NOW + timedelta(minutes=1)).isoformat()
    paper.market = PaperFixture.model_validate(value).market
    with pytest.raises(Unavailable):
        paper.submit(request(paper.account, "one", "margin_short", credit="general"), NOW)
    with pytest.raises(ValueError):
        paper.submit(
            request(paper.account, "two", "margin_cover", credit="general", position_id="invented"),
            NOW,
        )


def test_router_claim_without_receipt_blocks_entries_and_switch(tmp_path):
    from trading_system.execution.store import digest
    from trading_system.storage.store import canonical

    r = BrokerRouter(tmp_path, all_enabled(), fixture())
    try:
        req = request(r.active, "interrupted")
        with r.db:
            r.db.execute(
                "INSERT INTO routes VALUES(?,?,?)",
                (
                    req.internal_id,
                    digest(req.model_dump(mode="json")),
                    canonical(req.account.model_dump(mode="json")),
                ),
            )
        with pytest.raises(Unavailable, match="unresolved"):
            r.submit(request(r.active, "next"), NOW)
        with pytest.raises(Unavailable, match="unresolved"):
            r.switch("tachibana", NOW)
        assert r.active.broker == "rakuten"
        assert r.adapter.gateway.order_inventory() == []
    finally:
        r.close()


def test_switch_queries_both_accounts_even_with_pending_order(tmp_path, monkeypatch):
    r = BrokerRouter(tmp_path, all_enabled(), fixture())
    try:
        r.submit(request(r.active, "one"), NOW)
        target = r._adapter(r.config.account("tachibana"))
        calls = []
        for adapter in (r.adapter, target):
            for name in ("orders", "positions"):
                original = getattr(adapter, name)

                def observe(now, original=original, name=name, broker=adapter.account.broker):
                    calls.append((broker, name))
                    return original(now)

                monkeypatch.setattr(adapter, name, observe)
        with pytest.raises(ValueError):
            r.switch("tachibana", NOW)
        assert set(calls) == {
            (b, n) for b in ("rakuten", "tachibana") for n in ("orders", "positions")
        }
    finally:
        r.close()


def test_switch_target_with_existing_position_is_blocked(tmp_path):
    r = BrokerRouter(tmp_path, all_enabled(), fixture())
    try:
        target = r._adapter(r.config.account("tachibana"))
        target.submit(request(target.account, "target-one"), NOW)
        target.simulate_fill("target-one", 100, "100", "tf1", NOW)
        with pytest.raises(ValueError, match="flat"):
            r.switch("tachibana", NOW)
        assert r.active.broker == "rakuten"
    finally:
        r.close()


def test_connection_failure_never_falls_back_or_submits(tmp_path, monkeypatch):
    import trading_system.brokers.router as module

    calls = []

    class Broken:
        def connect(self):
            calls.append("connect")
            raise ConnectionError("simulated")

        def close(self):
            calls.append("close")

        def submit(self, *args):
            calls.append("submit")

    monkeypatch.setattr(module, "build_paper", lambda *args: Broken())
    with pytest.raises(ConnectionError):
        BrokerRouter(tmp_path, Config(), fixture())
    assert calls == ["connect", "close"]


def test_default_ids_unique_and_original_strategy_intent_preserved():
    from trading_system.brokers.models import from_intent
    from trading_system.execution.demo import mock_intent

    account = Account(broker="rakuten", account_id="test")
    original = mock_intent("strategy-id")
    converted = from_intent(original, account)
    assert converted.intent() == original
    data = converted.model_dump(mode="json")
    data.pop("internal_id")
    assert (
        OrderRequest.model_validate(data).internal_id
        != OrderRequest.model_validate(data).internal_id
    )
    with pytest.raises(ValueError):
        from_intent(mock_intent("short", "short"), account)


def test_model_copy_cannot_bypass_order_or_live_validation(paper, tmp_path):
    unsafe = request(paper.account, "one").model_copy(update={"action": "margin_buy"})
    with pytest.raises(ValueError):
        paper.submit(unsafe, NOW)
    config = Config().model_copy(
        update={
            "trading": Trading().model_copy(update={"live_trading_enabled": True}),
        }
    )
    with pytest.raises(ValueError):
        BrokerRouter(tmp_path / "bad", config, fixture())


def test_router_cancel_queries_pinned_account_and_rejects_unknown_id(tmp_path):
    r = BrokerRouter(tmp_path, all_enabled(), fixture())
    try:
        r.submit(request(r.active, "one"), NOW)
        r.cancel("one", NOW)
        r.switch("tachibana", NOW)
        assert r.order("one", NOW).request.account.broker == "rakuten"
        assert r.cancel("one", NOW).state == "CANCELLED"
        with pytest.raises(Unavailable):
            r.cancel("missing", NOW)
        assert r.adapter.orders(NOW) == ()
    finally:
        r.close()


def test_repeated_fill_id_is_rejected_and_does_not_change_cash(paper):
    paper.submit(request(paper.account, "one"), NOW)
    paper.simulate_fill("one", 40, "100", "one-fill", NOW)
    before = paper.balance(NOW)
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        paper.simulate_fill("one", 40, "100", "one-fill", NOW)
    assert paper.balance(NOW) == before


def test_closed_lunch_and_missing_calendar_fail_closed(tmp_path):
    from trading_system.brokers.models import SessionWindow

    f = fixture()
    market = f.market.model_copy(
        update={
            "sessions": (
                SessionWindow(opens_at=NOW, closes_at=NOW + timedelta(minutes=10)),
                SessionWindow(
                    opens_at=NOW + timedelta(minutes=20), closes_at=NOW + timedelta(minutes=50)
                ),
            )
        }
    )
    p = PaperAdapter(tmp_path, Account(broker="rakuten", account_id="test"), f.risk, market)
    try:
        p.connect()
        with pytest.raises(ValueError, match="session"):
            p.submit(request(p.account, "one"), NOW + timedelta(minutes=15))
        assert p.gateway.order_inventory() == []
    finally:
        p.close()
