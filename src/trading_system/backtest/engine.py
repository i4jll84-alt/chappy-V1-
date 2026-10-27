"""Chronological, all-or-none opening execution of a fixed-quantity momentum baseline."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from trading_system.backtest.models import Scenario
from trading_system.storage.store import canonical, digest

VERSION = "synthetic_next_open_v2"
ZERO = Decimal(0)


@dataclass
class Position:
    code: str
    direction: int
    quantity: Decimal
    entry_price: Decimal
    entry_at: str
    entry_index: int
    commission: Decimal
    carry: Decimal = ZERO
    action_pnl: Decimal = ZERO
    fractional_pnl: Decimal = ZERO
    short_proceeds: Decimal = ZERO


class Blocked(Exception):
    pass


def encode(value):
    """Portable deterministic JSON values, with exact money represented as strings."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode(item) for item in value]
    return value


def run_backtest(scenario: Scenario | dict) -> dict:
    scenario = Scenario.model_validate(scenario)
    with localcontext() as context:
        context.prec = 38
        return _run(scenario)


def _run(scenario):
    cash = scenario.initial_cash
    positions, marks, pending = {}, {}, []
    orders, ledger, trades, curve, receivables = [], [], [], [], []
    seen_bars = []
    status, reason = "complete", None
    last_date = None
    benchmark_start = scenario.sessions[0].benchmark_close
    costs = {key: ZERO for key in ("commission", "borrow", "reverse_fee")}

    def post(at, kind, amount, code=None):
        nonlocal cash
        cash += amount
        ledger.append({"at": at, "kind": kind, "code": code, "amount": amount})

    def equity():
        return (
            cash
            + sum((p.direction * p.quantity * marks[p.code] for p in positions.values()), ZERO)
            + sum((r["amount"] for r in receivables), ZERO)
        )

    def reserve(rules):
        return sum(
            (
                p.short_proceeds + rules[p.code].margin_ratio * p.quantity * marks[p.code]
                for p in positions.values()
                if p.direction < 0
            ),
            ZERO,
        )

    def order(code, side, quantity, at):
        event = {
            "id": len(orders) + 1,
            "code": code,
            "side": side,
            "quantity": quantity,
            "requested_at": at,
            "execution_at": None,
            "status": "pending",
            "reason": None,
            "fill_price": None,
            "commission": ZERO,
        }
        orders.append(event)
        pending.append(event)

    for index, session in enumerate(scenario.sessions):
        at = session.open_at.isoformat()
        rules = {r.code: r for r in session.rules}
        bars = {bar.code: bar for bar in session.bars}
        try:
            # Financing uses the previous close liability and actual elapsed calendar days.
            if last_date is not None:
                days = (session.date - last_date).days
                for position in positions.values():
                    if position.direction < 0:
                        rule = rules[position.code]
                        borrow = (
                            position.quantity
                            * marks[position.code]
                            * rule.borrow_annual_rate
                            * days
                            / rule.borrow_day_count
                        )
                        reverse = position.quantity * rule.reverse_fee_per_share_per_day * days
                        position.carry += borrow + reverse
                        costs["borrow"] += borrow
                        costs["reverse_fee"] += reverse
                        post(at, "borrow", -borrow, position.code)
                        post(at, "reverse_fee", -reverse, position.code)
            for action in scenario.actions:
                if action.effective_date != session.date:
                    continue
                position = positions.get(action.code)
                if action.kind == "split":
                    for event in list(pending):
                        if event["code"] == action.code:
                            event.update(
                                status="cancelled", reason="corporate_action", execution_at=at
                            )
                            pending.remove(event)
                    if position is None:
                        continue
                    adjusted_price = position.entry_price / action.ratio
                    exact_quantity = position.quantity * action.ratio
                    quantity = exact_quantity.to_integral_value(rounding=ROUND_FLOOR)
                    fraction = exact_quantity - quantity
                    if fraction:
                        if action.cash_in_lieu_price is None or quantity == 0:
                            raise Blocked("unsupported_fractional_corporate_action")
                        post(
                            at,
                            "cash_in_lieu",
                            position.direction * fraction * action.cash_in_lieu_price,
                            action.code,
                        )
                        position.fractional_pnl += (
                            position.direction
                            * fraction
                            * (action.cash_in_lieu_price - adjusted_price)
                        )
                        if position.direction < 0:
                            position.short_proceeds *= quantity / exact_quantity
                    position.quantity = quantity
                    position.entry_price = adjusted_price
                    marks[action.code] /= action.ratio
                    ledger.append(
                        {
                            "at": at,
                            "kind": "split",
                            "code": action.code,
                            "amount": ZERO,
                            "ratio": action.ratio,
                        }
                    )
                elif position is not None:
                    amount = position.direction * position.quantity * action.amount_per_share
                    position.action_pnl += amount
                    receivables.append(
                        {"code": action.code, "amount": amount, "pay_date": action.pay_date}
                    )
                    ledger.append(
                        {
                            "at": at,
                            "kind": action.kind + "_entitlement",
                            "code": action.code,
                            "amount": amount,
                        }
                    )
            for entitlement in list(receivables):
                if entitlement["pay_date"] == session.date:
                    post(at, "cash_entitlement_payment", entitlement["amount"], entitlement["code"])
                    receivables.remove(entitlement)
            # Execution truth is private to the engine, never passed to the strategy.
            for code, bar in bars.items():
                if bar.open is not None:
                    marks[code] = bar.open
            if positions and cash < reserve(rules):
                raise Blocked("unsupported_short_margin_breach")
            consumed = dict.fromkeys(rules, ZERO)
            for event in sorted(
                pending, key=lambda e: (e["side"] not in {"sell", "cover"}, e["id"])
            ):
                if event["requested_at"] >= at:
                    raise Blocked("order_not_strictly_before_execution")
                pending.remove(event)
                code, side, quantity = event["code"], event["side"], event["quantity"]
                rule, bar = rules[code], bars[code]
                event["execution_at"] = at
                buy = side in {"buy", "cover"}
                failure = None
                if quantity % rule.lot_size:
                    failure = "invalid_trading_unit"
                elif bar.open is None:
                    failure = "opening_price_unavailable"
                elif not (rule.can_buy if buy else rule.can_sell):
                    failure = "side_not_executable"
                elif consumed[code] + quantity > rule.opening_executable_quantity:
                    failure = "opening_liquidity_insufficient"
                elif side == "short" and not rule.short_allowed:
                    failure = "short_sale_restricted"
                elif side == "short" and quantity > rule.short_inventory:
                    failure = "short_inventory_insufficient"
                if failure is None:
                    shifted = bar.open + (1 if buy else -1) * rule.slippage_ticks * rule.tick_size
                    tick_rounding = ROUND_CEILING if buy else ROUND_FLOOR
                    price = (shifted / rule.tick_size).to_integral_value(
                        rounding=tick_rounding
                    ) * rule.tick_size
                    if not rule.limit_lower <= price <= rule.limit_upper:
                        failure = "execution_outside_price_band"
                if failure is None:
                    notional = quantity * price
                    fee = max(rule.minimum_commission, notional * rule.commission_rate)
                    spendable = cash - reserve(rules)
                    if side in {"buy", "short"}:
                        allocation = (
                            quantity * marks[code] * rule.margin_ratio
                            if side == "short"
                            else notional
                        )
                        if (
                            allocation
                            > max(equity(), ZERO) * scenario.strategy.max_allocation_fraction
                        ):
                            failure = "allocation_limit"
                        elif spendable < allocation + fee:
                            failure = "cash_or_collateral_insufficient"
                    elif side == "cover" and cash < notional + fee:
                        raise Blocked("unsupported_short_margin_breach")
                if failure is not None:
                    event.update(status="rejected", reason=failure)
                    continue
                event.update(status="filled", fill_price=price, commission=fee)
                consumed[code] += quantity
                costs["commission"] += fee
                post(at, "commission", -fee, code)
                if side in {"buy", "short"}:
                    direction = 1 if side == "buy" else -1
                    post(at, "entry_notional", -direction * notional, code)
                    positions[code] = Position(
                        code=code,
                        direction=direction,
                        quantity=quantity,
                        entry_price=price,
                        entry_at=at,
                        entry_index=index,
                        commission=fee,
                        short_proceeds=notional if direction < 0 else ZERO,
                    )
                else:
                    position = positions.pop(code)
                    post(at, "exit_notional", position.direction * notional, code)
                    gross = position.direction * quantity * (price - position.entry_price)
                    net = (
                        gross
                        + position.action_pnl
                        + position.fractional_pnl
                        - position.commission
                        - fee
                        - position.carry
                    )
                    trades.append(
                        {
                            "code": code,
                            "direction": "long" if position.direction > 0 else "short",
                            "entry_at": position.entry_at,
                            "exit_at": at,
                            "quantity": quantity,
                            "gross_pnl": gross,
                            "action_pnl": position.action_pnl + position.fractional_pnl,
                            "net_pnl": net,
                            "holding_sessions": index - position.entry_index,
                        }
                    )
            if any(bars[code].known_at > session.decision_at for code in positions):
                raise Blocked("held_position_close_unavailable")
            for bar in session.bars:
                marks[bar.code] = bar.close
                seen_bars.append((session.date, bar))
            if positions and cash < reserve(rules):
                raise Blocked("unsupported_short_margin_breach")
            current_equity = equity()
            if current_equity <= 0:
                raise Blocked("nonpositive_equity")
            gross_exposure = sum((p.quantity * marks[p.code] for p in positions.values()), ZERO)
            curve.append(
                {
                    "at": session.decision_at.isoformat(),
                    "cash": cash,
                    "equity": current_equity,
                    "gross_exposure": gross_exposure,
                    "restricted_cash": reserve(rules),
                    "free_cash": cash - reserve(rules),
                    "receivables": sum((r["amount"] for r in receivables), ZERO),
                    "benchmark_close": session.benchmark_close,
                }
            )
            # Only present/past visible closes and the dated declared universe reach signals.
            for code in sorted(set(session.universe) | positions.keys()):
                position = positions.get(code)
                if position is not None:
                    if index - position.entry_index + 1 >= scenario.strategy.holding_sessions:
                        order(
                            code,
                            "sell" if position.direction > 0 else "cover",
                            position.quantity,
                            session.decision_at.isoformat(),
                        )
                    continue
                if scenario.signal_policy == "supplied":
                    signal = next(
                        (
                            item
                            for item in scenario.signals
                            if item.code == code and item.decision_at == session.decision_at
                        ),
                        None,
                    )
                    if signal is not None and signal.direction:
                        order(
                            code,
                            "buy" if signal.direction > 0 else "short",
                            Decimal(scenario.strategy.quantity),
                            session.decision_at.isoformat(),
                        )
                    continue
                visible = [
                    (day, bar)
                    for day, bar in seen_bars
                    if bar.code == code and bar.known_at <= session.decision_at
                ]
                # The current close must itself be available; stale closes cannot trigger orders.
                if len(visible) < 2 or visible[-1][0] != session.date:
                    continue
                adjusted = []
                for day, bar in visible[-2:]:
                    close = bar.close
                    for action in scenario.actions:
                        if (
                            action.kind == "split"
                            and action.code == code
                            and day < action.effective_date <= session.date
                            and action.known_at <= session.decision_at
                        ):
                            close /= action.ratio
                    adjusted.append(close)
                if adjusted[0] != adjusted[1]:
                    order(
                        code,
                        "buy" if adjusted[1] > adjusted[0] else "short",
                        Decimal(scenario.strategy.quantity),
                        session.decision_at.isoformat(),
                    )
            last_date = session.date
        except Blocked as exc:
            status, reason = "blocked", str(exc)
            break
    for event in orders:
        if event["status"] == "pending":
            event.update(
                status="unfilled",
                reason=("simulation_blocked:" + reason)
                if status == "blocked"
                else "end_of_supplied_sessions",
            )
    from trading_system.backtest.metrics import summarize

    content = encode(
        {
            "scenario": scenario.model_dump(mode="json"),
            "engine_version": VERSION,
            "mode": scenario.mode,
            "status": status,
            "reason": reason,
            "source_label": scenario.source_label,
            "input_hash": digest(canonical(scenario.model_dump(mode="json")).encode()),
            "assumptions": {
                "strategy": (
                    "fixed_quantity_two_close_momentum"
                    if scenario.signal_policy == "momentum"
                    else "fixed_quantity_supplied_prediction_direction"
                ),
                "execution": "next_session_open_all_or_none",
                "short_collateral": "sale_proceeds_locked_plus_full_margin_collateral",
                "carry": "previous_close_notional_calendar_days_explicit_day_count",
                "corporate_actions": "explicit_events_cash_equivalent_rights_only",
                "end_positions": "marked_unliquidated",
                "historical_market_validity": False,
                "benchmark_kind": "synthetic_proxy",
                "risk_free_annual_rate": scenario.risk_free_annual_rate,
                "sessions_per_year": scenario.sessions_per_year,
            },
            "orders": orders,
            "ledger": ledger,
            "trades": trades,
            "equity_curve": curve,
            "positions": [
                vars(p) | {"mark": marks[p.code]}
                for p in sorted(positions.values(), key=lambda p: p.code)
            ],
            "unpaid_entitlements": receivables,
            "metrics": summarize(
                curve,
                trades,
                scenario.initial_cash,
                costs,
                scenario.risk_free_annual_rate,
                scenario.sessions_per_year,
                benchmark_start,
            )
            if status == "complete"
            else None,
        }
    )
    content["result_hash"] = digest(canonical(content).encode())
    return content
