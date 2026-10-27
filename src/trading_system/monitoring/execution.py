"""Observe a consistent SQLite MOCK snapshot without recovering or changing it."""

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

from trading_system.execution.models import MockConfig, OrderIntent
from trading_system.execution.risk import TERMINAL, check_risk
from trading_system.point_in_time.policy import aware_utc
from trading_system.storage.store import canonical

STATES = {
    "PREPARED",
    "DISPATCHING",
    "UNKNOWN_SUBMIT",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "FILLED",
    "REJECTED",
    "CANCEL_REQUESTED",
    "UNKNOWN_CANCEL",
    "CANCELLED",
}
UNCERTAIN = {"DISPATCHING", "UNKNOWN_SUBMIT", "CANCEL_REQUESTED", "UNKNOWN_CANCEL"}
ORDER_FIELDS = {
    "client_id",
    "intent",
    "fingerprint",
    "state",
    "filled_quantity",
    "broker_revision",
    "broker_hash",
    "broker_id",
    "send_attempt_count",
}
SCHEMA = {
    "metadata": {"key": ("TEXT", 1), "value": ("TEXT", 0)},
    "orders": {"client_id": ("TEXT", 1), "value": ("TEXT", 0)},
    "fills": {"fill_id": ("TEXT", 1), "value": ("TEXT", 0)},
    "events": {"sequence": ("INTEGER", 1), "kind": ("TEXT", 0), "value": ("TEXT", 0)},
}


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _require(condition):
    if not condition:
        raise ValueError("Invalid MOCK snapshot")


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _decimal(value):
    _require(isinstance(value, str) and bool(value))
    number = Decimal(value)
    _require(number.is_finite())
    return number


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _json(value):
    def reject_constant(_):
        raise ValueError("Nonfinite JSON")

    result = json.loads(value, object_pairs_hook=_object, parse_constant=reject_constant)
    _require(type(result) is dict)
    return result


def _read_snapshot(path):
    # mode=ro refuses missing databases. immutable=1 would miss active WAL commits.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = dict(
            connection.execute(
                "SELECT name,type FROM sqlite_master "
                "WHERE name IN ('metadata','orders','fills','events')"
            )
        )
        _require(tables == dict.fromkeys(SCHEMA, "table"))
        for table, expected in SCHEMA.items():
            actual = {
                r[1]: (r[2].upper(), r[5])
                for r in connection.execute(f'PRAGMA table_info("{table}")')
            }
            _require(actual == expected)
        snapshot = {
            "metadata": list(connection.execute("SELECT key,value FROM metadata ORDER BY key")),
            "orders": list(
                connection.execute("SELECT client_id,value FROM orders ORDER BY client_id")
            ),
            "fills": list(connection.execute("SELECT fill_id,value FROM fills ORDER BY fill_id")),
            "events": list(
                connection.execute("SELECT sequence,kind,value FROM events ORDER BY sequence")
            ),
        }
        connection.execute("COMMIT")
        return snapshot
    finally:
        connection.close()


def _validated(snapshot):
    metadata = {key: _json(value) for key, value in snapshot["metadata"]}
    _require(set(metadata) == {"config", "account", "halt"})
    config = MockConfig.model_validate(metadata["config"])
    _require(set(metadata["config"]) == set(MockConfig.model_fields))
    codes = {rule.code for rule in config.rules}
    account, halt = metadata["account"], metadata["halt"]
    _require(set(account) == {"cash", "positions"})
    _decimal(account["cash"])
    _require(type(account["positions"]) is dict and set(account["positions"]) <= codes)
    _require(all(type(q) is int for q in account["positions"].values()))
    _require(
        set(halt) == {"emergency", "uncertainty"} and all(type(v) is bool for v in halt.values())
    )
    orders, intents = {}, {}
    for key, value in snapshot["orders"]:
        order = _json(value)
        _require(set(order) == ORDER_FIELDS)
        intent = OrderIntent.model_validate(order["intent"])
        _require(key == order["client_id"] == intent.client_id and intent.code in codes)
        _require(order["fingerprint"] == _hash(intent.model_dump(mode="json")))
        _require(order["state"] in STATES)
        _require(_integer(order["filled_quantity"]) and order["filled_quantity"] <= intent.quantity)
        _require(
            _integer(order["broker_revision"])
            and _integer(order["send_attempt_count"])
            and order["send_attempt_count"] <= 1
        )
        if order["broker_revision"]:
            _require(isinstance(order["broker_id"], str) and bool(order["broker_id"]))
            _require(
                isinstance(order["broker_hash"], str)
                and re.fullmatch(r"[a-f0-9]{64}", order["broker_hash"]) is not None
            )
            _require(order["send_attempt_count"] == 1)
        else:
            _require(
                order["broker_id"] is None
                and order["broker_hash"] is None
                and order["filled_quantity"] == 0
            )
        state, filled = order["state"], order["filled_quantity"]
        if state == "PREPARED":
            _require(order["send_attempt_count"] == 0 and order["broker_revision"] == 0)
        elif state == "CANCELLED" and order["send_attempt_count"] == 0:
            _require(order["broker_revision"] == 0)
        else:
            _require(order["send_attempt_count"] == 1)
        if (
            state
            in {
                "ACKNOWLEDGED",
                "PARTIALLY_FILLED",
                "FILLED",
                "REJECTED",
                "CANCELLED",
                "CANCEL_REQUESTED",
                "UNKNOWN_CANCEL",
            }
            and order["send_attempt_count"]
        ):
            _require(order["broker_revision"] > 0)
        if state in {"PREPARED", "ACKNOWLEDGED", "REJECTED", "DISPATCHING"}:
            _require(filled == 0)
        if state == "FILLED":
            _require(filled == intent.quantity)
        elif state == "PARTIALLY_FILLED":
            _require(0 < filled < intent.quantity)
        elif state not in TERMINAL:
            _require(filled < intent.quantity)
        orders[key], intents[key] = order, intent
    fills = {}
    totals = Counter()
    for key, value in snapshot["fills"]:
        fill = _json(value)
        _require(set(fill) == {"client_id", "fill_id", "quantity", "price", "fee"})
        _require(
            isinstance(key, str)
            and bool(key)
            and key == fill["fill_id"]
            and fill["client_id"] in orders
        )
        _require(
            _integer(fill["quantity"], 1)
            and _decimal(fill["price"]) > 0
            and _decimal(fill["fee"]) >= 0
        )
        totals[fill["client_id"]] += fill["quantity"]
        fills[key] = fill
    _require(all(totals[key] == order["filled_quantity"] for key, order in orders.items()))
    events = []
    latest_reconcile = None
    for sequence, kind, value in snapshot["events"]:
        _require(_integer(sequence, 1) and isinstance(kind, str) and bool(kind))
        event = {"sequence": sequence, "kind": kind, "value": _json(value)}
        events.append(event)
        if kind == "account_reconciled":
            _require(set(event["value"]) == {"matches"} and type(event["value"]["matches"]) is bool)
            latest_reconcile = {
                "sequence": sequence,
                "matches": event["value"]["matches"],
                "checked_at": None,
                "historical_only": True,
                "current_proof": False,
            }
    _require(bool(events) and events[0]["kind"] == "initialized")
    _require(events[0]["value"] == {"config_hash": _hash(config.model_dump(mode="json"))})
    return config, account, halt, orders, intents, fills, events, latest_reconcile


def _account_matches_fills(config, account, intents, fills):
    # Exact aggregate invariant, not chronological execution replay or broker proof.
    expected_cash = Fraction(config.initial_cash)
    expected_positions = Counter(config.initial_positions)
    for fill in fills.values():
        intent = intents[fill["client_id"]]
        direction = 1 if intent.side in {"buy", "cover"} else -1
        expected_cash -= direction * fill["quantity"] * Fraction(_decimal(fill["price"]))
        expected_cash -= Fraction(_decimal(fill["fee"]))
        expected_positions[intent.code] += direction * fill["quantity"]
    return expected_cash == Fraction(_decimal(account["cash"])) and {
        code: quantity for code, quantity in expected_positions.items() if quantity
    } == {code: quantity for code, quantity in account["positions"].items() if quantity}


def monitor_execution(path: Path, now: datetime) -> dict:
    """Read CURRENT stored state; an injected clock does not reconstruct past state.

    Invalid or unavailable sources raise a generic ValueError without raw SQLite,
    validation details, database paths, or source payloads. No mutation is attempted.
    """
    try:
        now = aware_utc(now)
        snapshot = _read_snapshot(Path(path))
        config, account, halt, orders, intents, fills, events, latest_reconcile = _validated(
            snapshot
        )
        alerts = []

        def alert(code, **details):
            alerts.append({"code": code, **details})

        age = (now - config.evidence_at).total_seconds()
        evidence_status = (
            "future"
            if age < 0
            else "stale"
            if age > config.max_evidence_age_seconds
            else "within_declared_age"
        )
        if evidence_status != "within_declared_age":
            alert("account_evidence_" + evidence_status)
        rules = []
        for rule in config.rules:
            status = (
                "future"
                if rule.known_at > now or rule.valid_from > now
                else "expired"
                if now >= rule.valid_until
                else "within_declared_interval"
            )
            if status != "within_declared_interval":
                alert("rule_evidence_" + status, instrument_code=rule.code)
            rules.append(
                {
                    "code": rule.code,
                    "known_at": rule.known_at.isoformat(),
                    "valid_from": rule.valid_from.isoformat(),
                    "valid_until": rule.valid_until.isoformat(),
                    "status": status,
                }
            )
        for order in orders.values():
            intent = intents[order["client_id"]]
            if order["state"] in UNCERTAIN:
                alert("order_outcome_uncertain", client_id=intent.client_id, state=order["state"])
            if order["state"] not in TERMINAL and intent.expires_at <= now:
                alert("active_order_expired", client_id=intent.client_id)
            if intent.decision_at > now:
                alert("order_decision_in_future", client_id=intent.client_id)
            if order["state"] == "REJECTED":
                alert("historical_order_rejected", client_id=intent.client_id)
        for kind, enabled in halt.items():
            if enabled:
                alert(kind + "_halt_set")
        if latest_reconcile and latest_reconcile["matches"] is False:
            alert("historical_account_reconciliation_failed")
        if latest_reconcile is None and any(
            order["send_attempt_count"] for order in orders.values()
        ):
            alert("historical_account_reconciliation_missing")
        account_matches = _account_matches_fills(config, account, intents, fills)
        if not account_matches:
            alert("stored_account_fill_mismatch")
        risk = None
        try:
            risk = check_risk(config, account, list(orders.values()), now)
        except ValueError:
            alert("stored_resource_or_evidence_check_failed")
        valuation_available = (
            account_matches
            and evidence_status == "within_declared_age"
            and all(
                rule["status"] == "within_declared_interval"
                for rule in rules
                if account["positions"].get(rule["code"], 0)
            )
        )
        with localcontext() as context:
            context.prec = 38
            cash = _decimal(account["cash"])
            marks = {rule.code: rule.mark for rule in config.rules}
            equity = cash + sum(
                (quantity * marks[code] for code, quantity in account["positions"].items()),
                Decimal(0),
            )
            _require(equity.is_finite())
        if not account_matches:
            risk = None
        counts = Counter(order["state"] for order in orders.values())
        result = {
            "schema_version": 1,
            "status": "attention" if alerts else "observed",
            "mode": "mock",
            "read_only": True,
            "real_execution_enabled": False,
            "current_broker_connection_verified": False,
            "operational_readiness": False,
            "historical_market_validity": False,
            "observed_at": now.isoformat(),
            "state_semantics": "current_stored_snapshot_not_historical_pit",
            "config_hash": _hash(config.model_dump(mode="json")),
            "evidence": {
                "account_evidence_at": config.evidence_at.isoformat(),
                "age_seconds": age,
                "max_age_seconds": config.max_evidence_age_seconds,
                "status": evidence_status,
                "rules": rules,
            },
            "orders": {
                "count": len(orders),
                "by_state": dict(sorted(counts.items())),
                "active_count": sum(order["state"] not in TERMINAL for order in orders.values()),
                "filled_quantity": sum(order["filled_quantity"] for order in orders.values()),
            },
            "account": {
                "basis": "stored_mock_cash_and_fixed_synthetic_marks",
                "cash": account["cash"],
                "positions": account["positions"],
                "fixed_marked_synthetic_equity": str(equity) if valuation_available else None,
                "valuation_status": "available_synthetic_fixed_marks"
                if valuation_available
                else "unavailable_evidence_or_account_inconsistency",
                "aggregate_fill_consistency": account_matches,
                "realized_pnl": None,
                "unrealized_pnl": None,
                "drawdown": None,
            },
            "stored_resource_observation": risk,
            "resource_observation_scope": "fixed_mark_cash_reservations_not_full_pretrade_risk",
            "trade_permission": None,
            "halt": halt,
            "alerts": alerts,
            "latest_account_reconciliation": latest_reconcile,
            "source": {
                "kind": "sqlite_read_transaction",
                "snapshot_hash": _hash(snapshot),
                "event_count": len(events),
                "latest_event_sequence": events[-1]["sequence"],
                "fill_count": len(fills),
            },
        }
        result["result_hash"] = _hash(result)
        return result
    except Exception:
        raise ValueError("MOCK monitoring source unavailable or invalid") from None
