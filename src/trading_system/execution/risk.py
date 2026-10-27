"""Fixed-mark mock reservations and exact four-side cash accounting."""

from datetime import datetime
from decimal import Decimal

from trading_system.execution.models import OrderIntent

TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}


def rule_for(config, code):
    return next(rule for rule in config.rules if rule.code == code)


def apply_fill(account, intent, quantity, price, fee):
    account = {"cash": account["cash"], "positions": dict(account["positions"])}
    cash, price, fee = Decimal(account["cash"]), Decimal(price), Decimal(fee)
    old = account["positions"].get(intent.code, 0)
    if intent.side in {"buy", "cover"}:
        cash -= quantity * price + fee
        new = old + quantity
    else:
        cash += quantity * price - fee
        new = old - quantity
    if intent.side == "sell" and (old < quantity or new < 0):
        raise ValueError("Oversell")
    if intent.side == "cover" and (old > -quantity or new > 0):
        raise ValueError("Overcover")
    if intent.side == "buy" and old < 0 or intent.side == "short" and old > 0:
        raise ValueError("Unsupported reversal")
    account["cash"] = str(cash)
    account["positions"][intent.code] = new
    return account


def check_risk(config, account, orders, now: datetime, candidate=None):
    """Closing intents never credit hypothetical proceeds or release collateral."""
    if (
        now.tzinfo is None
        or not 0 <= (now - config.evidence_at).total_seconds() <= config.max_evidence_age_seconds
    ):
        raise ValueError("Unavailable/stale mock account evidence")
    active = [o for o in orders if o["state"] not in TERMINAL]
    if candidate is not None:
        active.append({"intent": candidate.model_dump(mode="json"), "filled_quantity": 0})
    positions = account["positions"]
    cash = Decimal(account["cash"])
    equity = cash
    locked = Decimal(0)
    gross = Decimal(0)
    shorts = Decimal(0)
    pending_new, pending_close = {}, {}
    for code, quantity in positions.items():
        r = rule_for(config, code)
        if quantity and not (r.known_at <= now and r.valid_from <= now < r.valid_until):
            raise ValueError("Unavailable held mock rule")
        equity += quantity * r.mark
        gross += abs(quantity) * r.mark
        if quantity < 0:
            locked += -quantity * r.mark * (1 + r.additional_collateral)
            shorts += -quantity * r.mark
    for order in active:
        intent = OrderIntent.model_validate(order["intent"])
        r = rule_for(config, intent.code)
        if not (r.known_at <= now and r.valid_from <= now < r.valid_until):
            raise ValueError("Unavailable pending mock rule")
        remaining = intent.quantity - order["filled_quantity"]
        fee = r.fee_fixed + r.fee_per_share * remaining
        if intent.side == "buy":
            locked += remaining * intent.limit_price + fee
        elif intent.side == "short":
            locked += (
                remaining * (r.additional_collateral * r.mark + max(r.mark - intent.limit_price, 0))
                + fee
            )
        elif intent.side == "cover":
            locked += max(
                Decimal(0),
                remaining * intent.limit_price
                + fee
                - (1 + r.additional_collateral) * r.mark * remaining,
            )
        else:
            locked += max(Decimal(0), fee - remaining * intent.limit_price)
        table = pending_new if intent.side in {"buy", "short"} else pending_close
        key = (intent.code, intent.side)
        table[key] = table.get(key, 0) + remaining
        if intent.side in {"buy", "short"}:
            gross += remaining * max(r.mark, intent.limit_price)
        if intent.side == "short":
            shorts += remaining * max(r.mark, intent.limit_price)
    for (code, side), quantity in pending_close.items():
        held = positions.get(code, 0)
        if quantity > (held if side == "sell" else -held):
            raise ValueError("Closing quantity already reserved")
    for r in config.rules:
        held = positions.get(r.code, 0)
        buys = pending_new.get((r.code, "buy"), 0)
        sells = pending_new.get((r.code, "short"), 0)
        if (held > 0 or buys) and (held < 0 or sells):
            raise ValueError("Opposite exposures are unsupported")
        new_risk = candidate is not None and candidate.side in {"buy", "short"}
        if new_risk and (
            max(held, 0) + buys > config.max_position_quantity
            or max(-held, 0) + sells > config.max_position_quantity
        ):
            raise ValueError("Position quantity limit")
        if new_risk and max(-held, 0) + sells > r.short_capacity:
            raise ValueError("Short capacity")
    if equity <= 0 or cash - locked < config.cash_floor:
        raise ValueError("Cash/collateral limit")
    if candidate is not None:
        r = rule_for(config, candidate.code)
        if not (r.known_at <= now and r.valid_from <= now < r.valid_until):
            raise ValueError("Unavailable mock rule")
        if not candidate.decision_at <= now < candidate.expires_at:
            raise ValueError("Expired/future intent")
        if candidate.quantity % r.lot_size or candidate.limit_price % r.tick_size:
            raise ValueError("Lot/tick violation")
        if not r.lower <= candidate.limit_price <= r.upper:
            raise ValueError("Price band")
        if (
            candidate.quantity > config.max_order_quantity
            or candidate.quantity * candidate.limit_price > config.max_order_value
        ):
            raise ValueError("Order limit")
        if (
            candidate.side in {"buy", "cover"}
            and not r.can_buy
            or candidate.side in {"sell", "short"}
            and not r.can_sell
        ):
            raise ValueError("Side restricted")
        if candidate.side == "short" and not r.short_allowed:
            raise ValueError("Short restricted")
        if candidate.side in {"buy", "short"}:
            if gross > config.max_gross_value or shorts > config.short_credit_limit:
                raise ValueError("Gross/credit limit")
            if equity <= config.day_start_equity * (1 - config.max_daily_loss_fraction):
                raise ValueError("Daily loss gate")
    return {
        "equity": str(equity),
        "restricted_cash": str(locked),
        "free_cash": str(cash - locked),
        "gross": str(gross),
        "short_value": str(shorts),
    }
