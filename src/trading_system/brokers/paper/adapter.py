"""Common contract over the existing durable mock gateway and risk manager."""

from contextlib import ExitStack
from decimal import Decimal

from trading_system.brokers.base import BrokerAdapter, Unavailable
from trading_system.brokers.capabilities import capabilities
from trading_system.brokers.locking import single_writer
from trading_system.brokers.models import (
    Balance,
    Connection,
    Costs,
    CostValue,
    Fill,
    Order,
    OrderRequest,
    PaperMarket,
    Position,
)
from trading_system.execution.mock import MockGateway
from trading_system.execution.risk import TERMINAL, check_risk, rule_for
from trading_system.execution.service import ExecutionManager
from trading_system.point_in_time.policy import aware_utc


class Adapter(BrokerAdapter):
    def __init__(self, root, account, risk, market: PaperMarket):
        if risk.initial_positions:
            raise ValueError("paper_bootstrap_requires_flat_account")
        self.account, self.risk, self.market = account, risk, market
        self.connected = False
        self.operation_guard = None
        self._resources = ExitStack()
        try:
            self._resources.enter_context(single_writer(root))
            self.gateway = MockGateway(root / "broker.sqlite", risk)
            self._resources.callback(self.gateway.close)
            self.manager = ExecutionManager(root / "manager.sqlite", risk, self.gateway)
            self._resources.callback(self.manager.close)
            binding = {
                "account": account.model_dump(mode="json"),
                "market": market.model_dump(mode="json"),
            }
            with self.manager.store.transaction():
                old = self.manager.store.get_meta("broker_binding")
                if old is not None and old != binding:
                    raise ValueError("immutable_account_or_market_binding")
                self.manager.store.set_meta("broker_binding", binding)
        except BaseException:
            self._resources.close()
            raise

    def capabilities(self):
        return capabilities(self.account.broker, paper=True)

    def connect(self):
        # Unknown acknowledgements must remain queryable after restart.
        # A mismatch persists the entry halt; connecting never clears it.
        self.manager.reconcile_account()
        self.connected = True
        return self.connection_status()

    def connection_status(self):
        state = "offline"
        if self.connected:
            state = (
                "reconciliation_required"
                if any(self.manager.store.get_meta("halt").values())
                else "paper_ready"
            )
        return Connection(account=self.account, state=state)

    def _ready(self):
        if not self.connected:
            raise Unavailable("connection", "offline")

    def _fresh(self, item, now):
        self._ready()
        now = aware_utc(now)
        if not 0 <= (now - item.known_at).total_seconds() <= self.market.max_age_seconds:
            raise Unavailable("evidence", "stale_or_future")
        return item

    def quote(self, code, now):
        item = next((q for q in self.market.quotes if q.code == code), None)
        if item is None:
            raise Unavailable("quote", "fixture_missing")
        return self._fresh(item, now)

    def board(self, code, now):
        item = next((q for q in self.market.boards if q.code == code), None)
        if item is None:
            raise Unavailable("board", "fixture_missing")
        return self._fresh(item, now)

    def short_availability(self, code, credit, now):
        item = next((q for q in self.market.shorts if (q.code, q.credit) == (code, credit)), None)
        if item is None:
            raise Unavailable("short_availability", "unverified_credit_inventory")
        self._fresh(item, now)
        if aware_utc(now) >= item.valid_until or item.allowed is None or item.quantity is None:
            raise Unavailable("short_availability", "unverified_credit_inventory")
        return item

    def _position_id(self, code, credit):
        return f"paper:{self.account.broker}:{self.account.account_id}:{code}:{credit or 'cash'}"

    def validate(self, request, now):
        request = OrderRequest.model_validate(request)
        now = aware_utc(now)
        self._ready()
        if request.action in {"cash_buy", "margin_short"} and any(
            self.manager.store.get_meta("halt").values()
        ):
            raise ValueError("new_entries_halted")
        if request.account != self.account:
            raise ValueError("wrong_account")
        self.market.require_open(now)
        if not self.manager.reconcile_account():
            raise ValueError("account_reconciliation_failed")
        if request.action in {"margin_short", "margin_cover"}:
            existing_credit = self.manager.store.get_meta("credit:" + request.code)
            held = self.gateway.account()["positions"].get(request.code, 0)
            active = [
                o
                for o in self.manager.store.orders()
                if o["state"] not in TERMINAL
                and o["intent"]["code"] == request.code
                and o["intent"]["side"] in {"short", "cover"}
            ]
            if (held < 0 or active) and existing_credit != request.credit:
                raise ValueError("credit_types_cannot_be_netted")
            if request.action == "margin_cover":
                if held >= 0 or request.position_id != self._position_id(
                    request.code, request.credit
                ):
                    raise ValueError("unknown_cover_position")
            else:
                inventory = self.short_availability(request.code, request.credit, now)
                reserved = sum(
                    o["intent"]["quantity"] - o["filled_quantity"]
                    for o in active
                    if o["intent"]["side"] == "short"
                )
                if (
                    not inventory.allowed
                    or max(-held, 0) + reserved + request.quantity > inventory.quantity
                ):
                    raise ValueError("short_inventory_unavailable")
        # Existing limits include lot/tick, margin, fees, closing reservations,
        # price bands, evidence expiry, gross exposure and daily loss.
        check_risk(
            self.risk,
            self.manager.store.get_meta("account"),
            self.manager.store.orders(),
            now,
            request.intent(),
        )

    def submit(self, request, now):
        request = OrderRequest.model_validate(request)
        if self.operation_guard:
            self.operation_guard("submit", request)
        now = aware_utc(now)
        self._ready()
        payload = request.model_dump(mode="json")
        with self.manager.store.transaction():
            old = self.manager.store.get_meta("request:" + request.internal_id)
            if old is not None and old != payload:
                raise ValueError("immutable_internal_id_conflict")
        existing = self.manager.store.order(request.internal_id)
        if existing is not None:
            if old is None:
                raise ValueError("missing_request_binding")
            # A repeated call is query-only, including PREPARED/unknown outcomes.
            return self.order(request.internal_id, now)
        self.validate(request, now)
        with self.manager.store.transaction():
            self.manager.store.set_meta("request:" + request.internal_id, payload)
            if request.action == "margin_short":
                self.manager.store.set_meta("credit:" + request.code, request.credit)
        self.manager.prepare(request.intent(), now)
        self.manager.dispatch(request.internal_id, now)
        return self.order(request.internal_id, now)

    def _view(self, item):
        request = OrderRequest.model_validate(
            self.manager.store.get_meta("request:" + item["client_id"])
        )
        broker = self.gateway.snapshot(item["client_id"])
        fills = tuple(
            Fill(
                account=self.account,
                internal_id=item["client_id"],
                broker_order_id=broker["broker_id"],
                evidence="simulated",
                **f,
            )
            for f in (broker["fills"] if broker else ())
        )
        return Order(
            request=request,
            broker_order_id=item["broker_id"],
            state=item["state"],
            filled_quantity=item["filled_quantity"],
            fills=fills,
        )

    def order(self, internal_id, now):
        self._ready()
        item = self.manager.store.order(internal_id)
        if item is None:
            return None
        if item["send_attempt_count"]:
            item = self.manager.reconcile(internal_id, aware_utc(now))
        return self._view(item)

    def orders(self, now):
        self._ready()
        return tuple(self.order(o["client_id"], now) for o in self.manager.store.orders())

    def cancel(self, internal_id, now):
        if self.operation_guard:
            self.operation_guard("cancel")
        self._ready()
        self.manager.cancel(internal_id, aware_utc(now))
        return self.order(internal_id, now)

    def modify(self, internal_id, price, quantity, now):
        if self.operation_guard:
            self.operation_guard("modify")
        raise Unavailable("modify", "unsupported_no_automatic_cancel_replace")

    def fills(self, now):
        return tuple(f for order in self.orders(now) for f in order.fills)

    def positions(self, now):
        self.orders(now)
        if not self.manager.reconcile_account():
            raise ValueError("account_reconciliation_failed")
        positions = []
        for code, signed in self.gateway.account()["positions"].items():
            if not signed:
                continue
            credit = self.manager.store.get_meta("credit:" + code) if signed < 0 else None
            if signed < 0 and credit is None:
                raise ValueError("missing_credit_binding")
            positions.append(
                Position(
                    account=self.account,
                    position_id=self._position_id(code, credit),
                    code=code,
                    quantity=abs(signed),
                    side="long" if signed > 0 else "short",
                    credit=credit,
                    evidence="simulated",
                )
            )
        return tuple(positions)

    def balance(self, now):
        self.positions(now)
        evidence = check_risk(
            self.risk,
            self.gateway.account(),
            self.manager.store.orders(),
            aware_utc(now),
        )
        return Balance(
            account=self.account,
            cash=self.gateway.account()["cash"],
            free_cash=evidence["free_cash"],
            short_credit_remaining=max(
                Decimal(0), self.risk.short_credit_limit - Decimal(evidence["short_value"])
            ),
            evidence="simulated",
        )

    def costs(self, code, now):
        self._ready()
        r = rule_for(self.risk, code)
        now = aware_utc(now)
        if not r.known_at <= now or not r.valid_from <= now < r.valid_until:
            raise Unavailable("costs", "expired_rule")

        def value(amount, unit):
            return CostValue(
                value=amount, evidence="estimated" if amount is not None else "unknown", unit=unit
            )

        return Costs(
            code=code,
            commission_fixed=value(r.fee_fixed, "JPY/order"),
            commission_per_share=value(r.fee_per_share, "JPY/share"),
            borrow_annual_rate=value(None, "fraction/year"),
            reverse_fee_per_share_day=value(None, "JPY/share/day"),
            spread=value(None, "JPY/share"),
            slippage=value(None, "JPY/share"),
            lot_size=r.lot_size,
            tick_size=r.tick_size,
            lower=r.lower,
            upper=r.upper,
            known_at=r.known_at,
            valid_until=r.valid_until,
        )

    def simulate_fill(self, internal_id, quantity, price, fill_id, now):
        """Explicit synthetic venue receipt; never infer fills from local intent."""
        self._ready()
        now = aware_utc(now)
        self.market.require_open(now)
        self.gateway.fill(internal_id, quantity, price, fill_id)
        return self.order(internal_id, now)

    def reconcile_account(self, now):
        self.orders(now)
        return self.manager.reconcile_account()

    def emergency_stop(self):
        self.manager.halt("operator_stop_new_entries")

    def disconnect(self):
        self.manager.halt("paper_connection_lost")
        self.connected = False

    def resume_entries(self, now):
        self._ready()
        self.manager.clear_halt(aware_utc(now))

    def close(self):
        self.connected = False
        self._resources.close()
