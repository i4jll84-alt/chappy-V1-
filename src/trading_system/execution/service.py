"""Durable before-send intent and conservative MOCK reconciliation state machine."""

import json
from decimal import Decimal
from functools import wraps

from trading_system.execution.models import OrderIntent
from trading_system.execution.risk import TERMINAL, apply_fill, check_risk, rule_for
from trading_system.execution.store import StateStore, digest
from trading_system.storage.store import canonical

UNCERTAIN = {"DISPATCHING", "UNKNOWN_SUBMIT", "CANCEL_REQUESTED", "UNKNOWN_CANCEL"}


def audited_request(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as error:
            first = args[0] if args else None
            client_id = (
                first.client_id
                if isinstance(first, OrderIntent)
                else first
                if isinstance(first, str)
                else None
            )
            reasons = {
                "Immutable idempotency key conflict": "idempotency_conflict",
                "Order limit": "order_limit",
                "Position quantity limit": "position_limit",
                "Cash/collateral limit": "cash_collateral_limit",
                "Closing quantity already reserved": "closing_capacity",
                "New entries halted": "entry_halt",
                "Daily loss gate": "daily_loss",
                "Short capacity": "short_capacity",
                "Gross/credit limit": "gross_credit_limit",
            }
            with self.store.transaction():
                self.store.event(
                    "request_failed",
                    {
                        "operation": method.__name__,
                        "client_id": client_id,
                        "reason_category": reasons.get(str(error), type(error).__name__),
                        "error_type": type(error).__name__,
                    },
                )
            raise

    return wrapped


class ExecutionManager:
    def __init__(self, path, config, gateway):
        self.config, self.gateway = config, gateway
        self.store = StateStore(path, config)
        with self.store.transaction():
            for order in self.store.orders():
                if order["state"] in {"DISPATCHING", "CANCEL_REQUESTED"}:
                    order["state"] = (
                        "UNKNOWN_SUBMIT" if order["state"] == "DISPATCHING" else "UNKNOWN_CANCEL"
                    )
                    self.store.save_order(order)
                    self._uncertain("restart_interrupted_operation", order["client_id"])

    def _uncertain(self, reason, client_id=None):
        halt = self.store.get_meta("halt")
        halt["uncertainty"] = True
        self.store.set_meta("halt", halt)
        self.store.event("uncertainty_halt", {"reason": reason, "client_id": client_id})

    def halt(self, reason):
        with self.store.transaction():
            halt = self.store.get_meta("halt")
            halt["emergency"] = True
            self.store.set_meta("halt", halt)
            self.store.event("emergency_halt", {"reason": reason})

    @audited_request
    def prepare(self, intent, now):
        intent = OrderIntent.model_validate(intent)
        payload = intent.model_dump(mode="json")
        with self.store.transaction():
            old = self.store.order(intent.client_id)
            if old:
                if old["intent"] != payload:
                    raise ValueError("Immutable idempotency key conflict")
                return old
            if intent.side in {"buy", "short"} and any(self.store.get_meta("halt").values()):
                raise ValueError("New entries halted")
            risk = check_risk(
                self.config, self.store.get_meta("account"), self.store.orders(), now, intent
            )
            order = {
                "client_id": intent.client_id,
                "intent": payload,
                "fingerprint": digest(payload),
                "state": "PREPARED",
                "filled_quantity": 0,
                "broker_revision": 0,
                "broker_hash": None,
                "broker_id": None,
                "send_attempt_count": 0,
            }
            self.store.save_order(order)
            self.store.event("intent_prepared", {"client_id": intent.client_id, "risk": risk})
        return order

    @audited_request
    def dispatch(self, client_id, now, **fault):
        existing = self.store.order(client_id)
        if existing and existing["intent"]["side"] in {"buy", "short"}:
            if not self.reconcile_account():
                raise ValueError("Account/inventory must reconcile before new submission")
        with self.store.transaction():
            order = self.store.order(client_id)
            if not order or order["state"] != "PREPARED":
                raise ValueError("Only a prepared intent may be dispatched; never retry unknown")
            intent = OrderIntent.model_validate(order["intent"])
            if intent.side in {"buy", "short"} and any(self.store.get_meta("halt").values()):
                raise ValueError("New entries halted")
            check_risk(
                self.config,
                self.store.get_meta("account"),
                [o for o in self.store.orders() if o["client_id"] != client_id],
                now,
                intent,
            )
            order["state"] = "DISPATCHING"
            order["send_attempt_count"] += 1
            self.store.save_order(order)
            self.store.event("dispatch_committed", {"client_id": client_id})
        try:
            snapshot = self.gateway.submit(intent, **fault)
            self.ingest(snapshot, now)
        except Exception:
            with self.store.transaction():
                order = self.store.order(client_id)
                if order["state"] not in TERMINAL:
                    order["state"] = "UNKNOWN_SUBMIT"
                    self.store.save_order(order)
                self._uncertain("submission_outcome_unknown", client_id)
            raise
        return self.store.order(client_id)

    @audited_request
    def cancel(self, client_id, now, **fault):
        with self.store.transaction():
            order = self.store.order(client_id)
            if order and order["state"] == "PREPARED":
                order["state"] = "CANCELLED"
                self.store.save_order(order)
                self.store.event("local_prepared_cancel", {"client_id": client_id})
                return order
            if not order or order["state"] not in {"ACKNOWLEDGED", "PARTIALLY_FILLED"}:
                raise ValueError("Cancellation requires a reconciled active order")
            order["state"] = "CANCEL_REQUESTED"
            self.store.save_order(order)
            self.store.event("cancel_committed", {"client_id": client_id})
        try:
            self.gateway.cancel(client_id, **fault)
            return self.reconcile(client_id, now)
        except Exception:
            with self.store.transaction():
                order = self.store.order(client_id)
                if order["state"] not in TERMINAL:
                    order["state"] = "UNKNOWN_CANCEL"
                    self.store.save_order(order)
                self._uncertain("cancellation_outcome_unknown", client_id)
            raise

    def reconcile(self, client_id, now):
        if not self.store.order(client_id):
            raise ValueError("Unknown client intent")
        try:
            snapshot = self.gateway.snapshot(client_id)
        except Exception:
            with self.store.transaction():
                self._uncertain("order_query_failed", client_id)
            raise
        if snapshot is None:
            with self.store.transaction():
                self._uncertain("broker_not_found_is_not_unsent", client_id)
            return self.store.order(client_id)
        return self.ingest(snapshot, now)

    def ingest(self, snapshot, now):
        """A whole authoritative cumulative snapshot is committed with unique fills."""
        try:
            with self.store.transaction():
                order = self.store.order(snapshot["client_id"])
                if (
                    not order
                    or order["state"] == "PREPARED"
                    or snapshot["intent"] != order["intent"]
                ):
                    raise ValueError("Unknown/unsubmitted/mismatched broker order")
                intent = OrderIntent.model_validate(order["intent"])
                if not isinstance(snapshot["broker_id"], str) or not snapshot["broker_id"]:
                    raise ValueError("Invalid broker identity")
                if isinstance(snapshot["filled_quantity"], bool) or not isinstance(
                    snapshot["filled_quantity"], int
                ):
                    raise ValueError("Invalid filled quantity type")
                revision = snapshot["revision"]
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                    raise ValueError("Invalid broker revision")
                payload_hash = digest(
                    {k: v for k, v in snapshot.items() if k != "send_attempt_count"}
                )
                if revision < order["broker_revision"]:
                    self.store.event(
                        "stale_snapshot_ignored",
                        {"client_id": intent.client_id, "revision": revision},
                    )
                    return order
                if revision == order["broker_revision"]:
                    if payload_hash != order["broker_hash"]:
                        raise ValueError("Contradictory same-revision snapshot")
                    return order
                if order["broker_id"] and order["broker_id"] != snapshot["broker_id"]:
                    raise ValueError("Changed broker identity")
                if order["state"] in TERMINAL and snapshot["state"] != order["state"]:
                    raise ValueError("Contradictory terminal state")
                if snapshot["state"] not in {
                    "ACKNOWLEDGED",
                    "PARTIALLY_FILLED",
                    "FILLED",
                    "CANCELLED",
                    "REJECTED",
                }:
                    raise ValueError("Unknown broker state")
                fills = snapshot["fills"]
                ids = [f["fill_id"] for f in fills]
                if any(not isinstance(key, str) or not key for key in ids):
                    raise ValueError("Invalid fill identity")
                total = sum(f["quantity"] for f in fills)
                if (
                    len(set(ids)) != len(ids)
                    or total != snapshot["filled_quantity"]
                    or not order["filled_quantity"] <= total <= intent.quantity
                ):
                    raise ValueError("Invalid cumulative fill quantity")
                if (
                    snapshot["state"] == "FILLED"
                    and total != intent.quantity
                    or snapshot["state"] == "REJECTED"
                    and total
                    or snapshot["state"] == "ACKNOWLEDGED"
                    and total
                    or snapshot["state"] == "PARTIALLY_FILLED"
                    and not 0 < total < intent.quantity
                ):
                    raise ValueError("Contradictory filled status")
                previous = {
                    r[0]
                    for r in self.store.db.execute(
                        "SELECT fill_id FROM fills WHERE json_extract(value,'$.client_id')=?",
                        (intent.client_id,),
                    )
                }
                if not previous <= set(ids):
                    raise ValueError("Broker removed a prior fill")
                if order["state"] in TERMINAL and (
                    total != order["filled_quantity"] or set(ids) != previous
                ):
                    raise ValueError("Terminal fill set changed")
                account = self.store.get_meta("account")
                rule = rule_for(self.config, intent.code)
                cumulative = 0
                for fill in fills:
                    if set(fill) != {"fill_id", "quantity", "price", "fee"}:
                        raise ValueError("Unexpected fill fields")
                    quantity, price, fee = (
                        fill["quantity"],
                        Decimal(fill["price"]),
                        Decimal(fill["fee"]),
                    )
                    if (
                        isinstance(quantity, bool)
                        or not isinstance(quantity, int)
                        or quantity <= 0
                        or quantity % rule.lot_size
                        or not price.is_finite()
                        or not fee.is_finite()
                        or price <= 0
                        or fee < 0
                        or price % rule.tick_size
                        or not rule.lower <= price <= rule.upper
                    ):
                        raise ValueError("Invalid broker fill")
                    expected_fee = quantity * rule.fee_per_share + (
                        rule.fee_fixed if cumulative == 0 else 0
                    )
                    if (
                        fee != expected_fee
                        or (intent.side in {"buy", "cover"} and price > intent.limit_price)
                        or (intent.side in {"sell", "short"} and price < intent.limit_price)
                    ):
                        raise ValueError("Fill violates immutable costs/limit")
                    cumulative += quantity
                    value = {"client_id": intent.client_id, **fill}
                    old = self.store.db.execute(
                        "SELECT value FROM fills WHERE fill_id=?", (fill["fill_id"],)
                    ).fetchone()
                    if old:
                        if json.loads(old[0]) != value:
                            raise ValueError("Changed/duplicate fill identity")
                    else:
                        account = apply_fill(account, intent, quantity, price, fee)
                        self.store.db.execute(
                            "INSERT INTO fills VALUES(?,?)", (fill["fill_id"], canonical(value))
                        )
                        self.store.event("fill_applied", value)
                order.update(
                    state=snapshot["state"],
                    filled_quantity=total,
                    broker_revision=revision,
                    broker_hash=payload_hash,
                    broker_id=snapshot["broker_id"],
                )
                self.store.save_order(order)
                self.store.set_meta("account", account)
                self.store.event(
                    "snapshot_applied",
                    {"client_id": intent.client_id, "revision": revision, "state": order["state"]},
                )
                # A real fill cannot be undone. Retain it, then halt on unsupported resources.
                try:
                    check_risk(self.config, account, self.store.orders(), now)
                except ValueError:
                    self._uncertain("post_fill_resource_or_evidence_breach", intent.client_id)
                return order
        except Exception:
            with self.store.transaction():
                self._uncertain("contradictory_broker_snapshot", snapshot.get("client_id"))
            raise

    def reconcile_account(self):
        try:
            matches = self.store.get_meta("account") == self.gateway.account()
            broker_orders = self.gateway.order_inventory()
            local = {o["client_id"]: o for o in self.store.orders() if o["send_attempt_count"] > 0}
            if len(broker_orders) != len(local):
                matches = False
            for broker in broker_orders:
                order = local.get(broker["client_id"])
                observed_hash = digest(
                    {k: v for k, v in broker.items() if k != "send_attempt_count"}
                )
                if (
                    not order
                    or order["broker_hash"] != observed_hash
                    or order["state"] != broker["state"]
                ):
                    matches = False
        except Exception:
            with self.store.transaction():
                self._uncertain("account_query_failed")
            raise
        with self.store.transaction():
            self.store.event("account_reconciled", {"matches": matches})
            if not matches:
                self._uncertain("account_mismatch")
        return matches

    @audited_request
    def clear_halt(self, now):
        if not self.reconcile_account():
            raise ValueError("Account mismatch")
        with self.store.transaction():
            if any(o["state"] in UNCERTAIN for o in self.store.orders()):
                raise ValueError("Unresolved order uncertainty")
            check_risk(self.config, self.store.get_meta("account"), self.store.orders(), now)
            self.store.set_meta("halt", {"emergency": False, "uncertainty": False})
            self.store.event("operator_clear_halt", {})

    def snapshot(self):
        result = {
            "mode": "mock",
            "real_execution_enabled": False,
            "config": self.config.model_dump(mode="json"),
            "config_hash": digest(self.config.model_dump(mode="json")),
            "orders": self.store.orders(),
            "account": self.store.get_meta("account"),
            "halt": self.store.get_meta("halt"),
            "events": self.store.events(),
        }
        result["result_hash"] = digest(result)
        return result

    def close(self):
        self.store.close()
