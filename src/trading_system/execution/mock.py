"""Durable independent, offline broker simulator with explicit fault injection."""

from decimal import Decimal

from trading_system.execution.models import OrderIntent
from trading_system.execution.risk import apply_fill, rule_for
from trading_system.execution.store import StateStore, digest


class MockGateway:
    def __init__(self, path, config):
        self.config = config
        self.store = StateStore(path, config)

    def submit(self, intent, *, lose_ack=False, reject=False):
        with self.store.transaction():
            old = self.store.order(intent.client_id)
            if old:
                if old["intent"] != intent.model_dump(mode="json"):
                    raise ValueError("Broker idempotency conflict")
                old["send_attempt_count"] += 1
                self.store.save_order(old)
                result = old
            else:
                result = {
                    "client_id": intent.client_id,
                    "broker_id": "MOCK-" + digest(intent.client_id)[:20],
                    "intent": intent.model_dump(mode="json"),
                    "state": "REJECTED" if reject else "ACKNOWLEDGED",
                    "revision": 1,
                    "fills": [],
                    "filled_quantity": 0,
                    "send_attempt_count": 1,
                }
                self.store.save_order(result)
                self.store.event("submit", result)
        if lose_ack:
            raise TimeoutError("Injected lost submission acknowledgement")
        return result

    def snapshot(self, client_id):
        return self.store.order(client_id)

    def fill(self, client_id, quantity, price, fill_id):
        price = Decimal(price)
        with self.store.transaction():
            order = self.store.order(client_id)
            if not order or order["state"] not in {"ACKNOWLEDGED", "PARTIALLY_FILLED"}:
                raise ValueError("Broker order is not fillable")
            intent = OrderIntent.model_validate(order["intent"])
            rule = rule_for(self.config, intent.code)
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity <= 0
                or quantity % rule.lot_size
            ):
                raise ValueError("Invalid fill quantity")
            if (
                quantity + order["filled_quantity"] > intent.quantity
                or not price.is_finite()
                or price <= 0
                or price % rule.tick_size
                or not rule.lower <= price <= rule.upper
            ):
                raise ValueError("Invalid fill")
            if (
                intent.side in {"buy", "cover"}
                and price > intent.limit_price
                or intent.side in {"sell", "short"}
                and price < intent.limit_price
            ):
                raise ValueError("Limit violation")
            fee = quantity * rule.fee_per_share + (rule.fee_fixed if not order["fills"] else 0)
            fill = {"fill_id": fill_id, "quantity": quantity, "price": str(price), "fee": str(fee)}
            self.store.db.execute(
                "INSERT INTO fills VALUES(?,?)", (fill_id, __import__("json").dumps(fill))
            )
            account = apply_fill(self.store.get_meta("account"), intent, quantity, price, fee)
            self.store.set_meta("account", account)
            order["fills"].append(fill)
            order["filled_quantity"] += quantity
            order["revision"] += 1
            order["state"] = (
                "FILLED" if order["filled_quantity"] == intent.quantity else "PARTIALLY_FILLED"
            )
            self.store.save_order(order)
            self.store.event("fill", {"client_id": client_id, **fill})
        return order

    def cancel(self, client_id, *, lose_ack=False):
        with self.store.transaction():
            order = self.store.order(client_id)
            if not order:
                raise ValueError("Unknown broker order")
            if order["state"] not in {"FILLED", "CANCELLED", "REJECTED"}:
                order["state"] = "CANCELLED"
                order["revision"] += 1
                self.store.save_order(order)
                self.store.event("cancel", {"client_id": client_id})
        if lose_ack:
            raise TimeoutError("Injected lost cancellation acknowledgement")
        return order

    def account(self):
        return self.store.get_meta("account")

    def order_inventory(self):
        return self.store.orders()

    def close(self):
        self.store.close()
