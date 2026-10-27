"""Chronological fixed-portfolio planning with opening execution and strategy-owned lots."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from trading_system.backtest.engine import encode
from trading_system.portfolio.planner import plan_portfolio
from trading_system.portfolio.simulation_models import SimulationInput
from trading_system.storage.store import canonical, digest

ZERO = Decimal(0)
VERSION = "synthetic_chronological_fixed_portfolio_v1"


@dataclass
class Lot:
    lot_id: str
    code: str
    strategy: str
    direction: int
    quantity: int
    entry_price: Decimal
    entry_at: str
    entry_index: int
    entry_fee: Decimal
    carry: Decimal = ZERO


class SimulationBlocked(Exception):
    pass


def run_portfolio_simulation(source: SimulationInput | dict) -> dict:
    source = SimulationInput.model_validate(source)
    with localcontext() as context:
        context.prec = 38
        return _run(source)


def _run(source):
    cash = source.initial_cash
    lots, marks, orders, ledger, trades, curve, plans, opening_checks = (
        [],
        {},
        [],
        [],
        [],
        [],
        [],
        [],
    )
    pending, planning_rejections, opening_halts = [], [], []
    strategies = {item.strategy: item for item in source.strategies}
    costs = {name: ZERO for name in ("commission", "borrow", "reverse_fee")}
    strategy_costs = {name: dict.fromkeys(costs, ZERO) for name in strategies}
    day_start = reference = source.initial_cash
    previous_date = None
    status, reason = "complete", None

    def post(at, kind, amount, code=None, strategy=None):
        nonlocal cash
        cash += amount
        ledger.append(
            {"at": at, "kind": kind, "amount": amount, "code": code, "strategy": strategy}
        )

    def equity():
        return cash + sum((lot.direction * lot.quantity * marks[lot.code] for lot in lots), ZERO)

    def positions(extra=None):
        grouped = {}
        for lot in lots:
            if lot.code not in grouped:
                grouped[lot.code] = {
                    "code": lot.code,
                    "direction": lot.direction,
                    "quantity": 0,
                    "allocations": [],
                }
            grouped[lot.code]["quantity"] += lot.quantity
            grouped[lot.code]["allocations"].append(
                {"strategy": lot.strategy, "quantity": lot.quantity}
            )
        result = list(grouped.values())
        if extra is not None:
            result.append(extra)
        return result

    def account(at, actual_cash):
        return {
            "cash": actual_cash,
            "as_of": at,
            "known_at": at,
            "day_start_equity": day_start,
            "reference_equity": reference,
            "source_id": "derived_synthetic_simulation_account",
        }

    def make_plan(
        at,
        instruments,
        correlations,
        *,
        signals=(),
        reservations=(),
        actual_cash=None,
        extra_position=None,
    ):
        return plan_portfolio(
            {
                "mode": "synthetic",
                "source_label": source.source_label,
                "decision_at": at,
                "account": account(at, cash if actual_cash is None else actual_cash),
                "instruments": instruments,
                "strategies": [
                    {"strategy": item.strategy, "weight": item.weight, "priority": item.priority}
                    for item in source.strategies
                ],
                "positions": positions(extra_position),
                "reservations": reservations,
                "signals": signals,
                "correlations": correlations,
                "limits": source.limits,
            }
        )

    def add_order(kind, code, direction, contributors, at, source_hash=None):
        event = {
            "id": len(orders) + 1,
            "kind": kind,
            "code": code,
            "direction": direction,
            "quantity": sum(item["quantity"] for item in contributors),
            "contributors": contributors,
            "requested_at": at,
            "execution_at": None,
            "status": "pending",
            "reason": None,
            "fill_price": None,
            "commission": ZERO,
            "source_plan_hash": source_hash,
        }
        orders.append(event)
        pending.append(event)

    def held_opening_rules_available(instruments, at):
        return all(
            instruments[lot.code].rule_known_at <= at
            and instruments[lot.code].valid_from <= at < instruments[lot.code].valid_until
            for lot in lots
        )

    def allocation_fee(total, contributors):
        quantity = sum(item["quantity"] for item in contributors)
        remaining = total
        allocations = []
        for index, item in enumerate(contributors):
            part = (
                remaining if index == len(contributors) - 1 else total * item["quantity"] / quantity
            )
            remaining -= part
            allocations.append(part)
        return allocations

    for index, (session, frame) in enumerate(
        zip(source.market.sessions, source.sessions, strict=True)
    ):
        at = session.open_at.isoformat()
        rules = {rule.code: rule for rule in session.rules}
        bars = {bar.code: bar for bar in session.bars}
        opening = {item.code: item for item in frame.opening_instruments}
        try:
            if previous_date is not None:
                days = (session.date - previous_date).days
                for lot in lots:
                    if lot.direction < 0:
                        rule = rules[lot.code]
                        borrow = (
                            lot.quantity
                            * marks[lot.code]
                            * rule.borrow_annual_rate
                            * days
                            / rule.borrow_day_count
                        )
                        reverse = lot.quantity * rule.reverse_fee_per_share_per_day * days
                        lot.carry += borrow + reverse
                        for name, amount in (("borrow", borrow), ("reverse_fee", reverse)):
                            costs[name] += amount
                            strategy_costs[lot.strategy][name] += amount
                            post(at, name, -amount, lot.code, lot.strategy)
            for bar in session.bars:
                if bar.open is not None:
                    marks[bar.code] = bar.open
            consumed = dict.fromkeys(bars, 0)
            queue = sorted(pending, key=lambda item: (item["kind"] != "exit", item["id"]))
            pending = []
            for event in queue:
                event["execution_at"] = at
                if event["requested_at"] >= at:
                    raise SimulationBlocked("order_not_strictly_before_execution")
                code, quantity = event["code"], event["quantity"]
                rule, bar = rules[code], bars[code]
                buying = (event["kind"] == "entry" and event["direction"] > 0) or (
                    event["kind"] == "exit" and event["direction"] < 0
                )
                failure = None
                if quantity % rule.lot_size:
                    failure = "invalid_trading_unit"
                elif bar.open is None:
                    failure = "opening_price_unavailable"
                elif not (rule.can_buy if buying else rule.can_sell):
                    failure = "side_not_executable"
                elif consumed[code] + quantity > rule.opening_executable_quantity:
                    failure = "opening_liquidity_insufficient"
                elif event["kind"] == "entry" and any(lot.code == code for lot in lots):
                    failure = "code_still_occupied"
                elif event["kind"] == "entry" and event["direction"] < 0 and not rule.short_allowed:
                    failure = "short_sale_restricted"
                if failure is None:
                    shifted = (
                        bar.open + (1 if buying else -1) * rule.slippage_ticks * rule.tick_size
                    )
                    rounding = ROUND_CEILING if buying else ROUND_FLOOR
                    price = (shifted / rule.tick_size).to_integral_value(
                        rounding=rounding
                    ) * rule.tick_size
                    if not rule.limit_lower <= price <= rule.limit_upper:
                        failure = "execution_outside_price_band"
                if failure is not None:
                    event.update(status="rejected", reason=failure)
                    continue
                notional = quantity * price
                commission = max(rule.minimum_commission, rule.commission_rate * notional)
                if event["kind"] == "exit":
                    exiting = [
                        lot
                        for lot in lots
                        if lot.lot_id in {item["lot_id"] for item in event["contributors"]}
                    ]
                    if sum(lot.quantity for lot in exiting) != quantity:
                        raise SimulationBlocked("exit_contributor_state_mismatch")
                    if event["direction"] < 0 and cash < notional + commission:
                        event.update(
                            status="rejected", reason="unsupported_short_cover_cash_deficit"
                        )
                        raise SimulationBlocked("unsupported_short_cover_cash_deficit")
                    fees = allocation_fee(commission, event["contributors"])
                    post(at, "exit_notional", event["direction"] * notional, code)
                    post(at, "commission", -commission, code)
                    for lot, fee in zip(exiting, fees, strict=True):
                        strategy_costs[lot.strategy]["commission"] += fee
                        gross = lot.direction * lot.quantity * (price - lot.entry_price)
                        trades.append(
                            {
                                "code": code,
                                "strategy": lot.strategy,
                                "quantity": lot.quantity,
                                "direction": lot.direction,
                                "entry_at": lot.entry_at,
                                "exit_at": at,
                                "entry_price": lot.entry_price,
                                "exit_price": price,
                                "gross_pnl": gross,
                                "entry_fee": lot.entry_fee,
                                "exit_fee": fee,
                                "carry": lot.carry,
                                "net_pnl": gross - lot.entry_fee - fee - lot.carry,
                                "holding_sessions": index - lot.entry_index,
                                "lot_id": lot.lot_id,
                            }
                        )
                        lots.remove(lot)
                else:
                    if not held_opening_rules_available(opening, session.open_at):
                        raise SimulationBlocked("held_opening_risk_evidence_unavailable")
                    if any(bars[lot.code].open is None for lot in lots):
                        event.update(status="rejected", reason="held_opening_mark_unavailable")
                        opening_halts.append({"at": at, "reason": "held_opening_mark_unavailable"})
                        continue
                    instrument = opening[code]
                    buffer = max(
                        commission, instrument.fee_fixed + instrument.fee_per_share * quantity
                    )
                    short_gap = (
                        max(ZERO, bar.open - price) * quantity if event["direction"] < 0 else ZERO
                    )
                    buffer += short_gap
                    allocations = [
                        {"strategy": item["strategy"], "quantity": item["quantity"]}
                        for item in event["contributors"]
                    ]
                    reservation = {
                        "code": code,
                        "direction": event["direction"],
                        "quantity": quantity,
                        "allocations": allocations,
                        "order_id": "queued-" + str(event["id"]),
                        "planning_price": max(price, bar.open),
                        "fee_buffer": buffer,
                        "known_at": at,
                    }
                    check = make_plan(
                        at,
                        frame.opening_instruments,
                        frame.opening_correlations,
                        reservations=[reservation],
                    )
                    opening_checks.append(
                        {
                            "order_id": event["id"],
                            "phase": "reservation",
                            "plan": check,
                            "actual_commission": commission,
                            "short_slippage_buffer": short_gap,
                        }
                    )
                    if check["status"] != "complete":
                        event.update(
                            status="rejected",
                            reason="opening_reservation_risk",
                            risk_reasons=check["blocked_reasons"],
                        )
                        continue
                    shadow_cash = cash - event["direction"] * notional - commission
                    shadow = make_plan(
                        at,
                        frame.opening_instruments,
                        frame.opening_correlations,
                        actual_cash=shadow_cash,
                        extra_position={
                            "code": code,
                            "direction": event["direction"],
                            "quantity": quantity,
                            "allocations": allocations,
                        },
                    )
                    opening_checks.append(
                        {"order_id": event["id"], "phase": "actual_post_fill", "plan": shadow}
                    )
                    if shadow["status"] != "complete":
                        event.update(
                            status="rejected",
                            reason="actual_post_fill_risk",
                            risk_reasons=shadow["blocked_reasons"],
                        )
                        continue
                    fees = allocation_fee(commission, event["contributors"])
                    post(at, "entry_notional", -event["direction"] * notional, code)
                    post(at, "commission", -commission, code)
                    for contributor, fee in zip(event["contributors"], fees, strict=True):
                        strategy = contributor["strategy"]
                        strategy_costs[strategy]["commission"] += fee
                        lots.append(
                            Lot(
                                lot_id=f"{index}:{code}:{strategy}",
                                code=code,
                                strategy=strategy,
                                direction=event["direction"],
                                quantity=contributor["quantity"],
                                entry_price=price,
                                entry_at=at,
                                entry_index=index,
                                entry_fee=fee,
                            )
                        )
                costs["commission"] += commission
                consumed[code] += quantity
                event.update(
                    status="filled",
                    fill_price=price,
                    commission=commission,
                    commission_allocations=[
                        {"strategy": item["strategy"], "amount": fee}
                        for item, fee in zip(event["contributors"], fees, strict=True)
                    ],
                )
            if not held_opening_rules_available(opening, session.open_at):
                raise SimulationBlocked("held_opening_risk_evidence_unavailable")
            if any(bars[lot.code].open is None for lot in lots):
                if not any(item["at"] == at for item in opening_halts):
                    opening_halts.append({"at": at, "reason": "held_opening_mark_unavailable"})
            if lots and all(bars[lot.code].open is not None for lot in lots):
                restricted = sum(
                    (
                        (1 + opening[lot.code].short_additional_collateral_ratio)
                        * lot.quantity
                        * marks[lot.code]
                        for lot in lots
                        if lot.direction < 0
                    ),
                    ZERO,
                )
                if cash < restricted or equity() <= 0:
                    raise SimulationBlocked("unsupported_opening_margin_or_equity_breach")
            if any(bars[lot.code].known_at > session.decision_at for lot in lots):
                raise SimulationBlocked("held_position_close_unavailable")
            for bar in session.bars:
                if bar.known_at <= session.decision_at:
                    marks[bar.code] = bar.close
            closing = {item.code: item for item in frame.decision_instruments}
            for lot in lots:
                instrument = closing[lot.code]
                if not (
                    instrument.rule_known_at <= session.decision_at
                    and instrument.valid_from <= session.decision_at < instrument.valid_until
                    and instrument.mark_as_of <= instrument.mark_known_at <= session.decision_at
                    and (session.decision_at - instrument.mark_as_of).total_seconds()
                    <= source.limits.max_evidence_age_seconds
                ):
                    raise SimulationBlocked("held_decision_evidence_unavailable")
            restricted = sum(
                (
                    (1 + closing[lot.code].short_additional_collateral_ratio)
                    * lot.quantity
                    * marks[lot.code]
                    for lot in lots
                    if lot.direction < 0
                ),
                ZERO,
            )
            if cash < restricted or equity() <= 0:
                raise SimulationBlocked("unsupported_closing_margin_or_equity_breach")
            current_equity = equity()
            curve.append(
                {
                    "at": session.decision_at.isoformat(),
                    "cash": cash,
                    "equity": current_equity,
                    "restricted_cash": restricted,
                    "available_cash": cash - restricted,
                    "long_gross": sum(
                        (lot.quantity * marks[lot.code] for lot in lots if lot.direction > 0), ZERO
                    ),
                    "short_gross": sum(
                        (lot.quantity * marks[lot.code] for lot in lots if lot.direction < 0), ZERO
                    ),
                }
            )
            due = {}
            for lot in lots:
                if index - lot.entry_index + 1 >= strategies[lot.strategy].holding_sessions:
                    due.setdefault(lot.code, []).append(lot)
            for code in sorted(due):
                contributors = [
                    {"strategy": lot.strategy, "quantity": lot.quantity, "lot_id": lot.lot_id}
                    for lot in due[code]
                ]
                add_order(
                    "exit",
                    code,
                    due[code][0].direction,
                    contributors,
                    session.decision_at.isoformat(),
                )
            eligible = []
            for signal in frame.signals:
                strategy = strategies.get(signal.strategy)
                if strategy is not None and index + 1 + strategy.holding_sessions >= len(
                    source.sessions
                ):
                    planning_rejections.append(
                        {
                            "decision_index": index,
                            "strategy": signal.strategy,
                            "code": signal.code,
                            "reason": "insufficient_execution_tail",
                        }
                    )
                else:
                    eligible.append(signal)
            plan = make_plan(
                session.decision_at,
                frame.decision_instruments,
                frame.decision_correlations,
                signals=eligible,
            )
            plans.append({"decision_index": index, "plan": plan})
            for proposal in plan["proposals"]:
                add_order(
                    "entry",
                    proposal["code"],
                    proposal["direction"],
                    proposal["contributors"],
                    session.decision_at.isoformat(),
                    plan["result_hash"],
                )
            day_start, reference, previous_date = (
                current_equity,
                max(reference, current_equity),
                session.date,
            )
        except SimulationBlocked as exc:
            status, reason = "blocked", str(exc)
            break
    for event in orders:
        if event["status"] == "pending":
            event.update(
                status="cancelled" if event["kind"] == "entry" else "unfilled",
                reason="simulation_blocked:" + reason
                if status == "blocked"
                else "end_of_supplied_sessions",
            )
    per_strategy = {}
    for strategy in strategies:
        owned = [lot for lot in lots if lot.strategy == strategy]
        realized = sum(
            (trade["net_pnl"] for trade in trades if trade["strategy"] == strategy), ZERO
        )
        unrealized = sum(
            (
                lot.direction * lot.quantity * (marks[lot.code] - lot.entry_price)
                - lot.entry_fee
                - lot.carry
                for lot in owned
            ),
            ZERO,
        )
        per_strategy[strategy] = {
            "realized_net_pnl": realized,
            "open_net_pnl": unrealized,
            "total_net_pnl": realized + unrealized,
            "costs": strategy_costs[strategy],
            "open_quantity": sum(lot.quantity for lot in owned),
        }
    net_pnl = sum((item["total_net_pnl"] for item in per_strategy.values()), ZERO)
    final_equity = equity()
    tolerance = Decimal("1e-24")
    residual = final_equity - source.initial_cash - net_pnl
    cost_residuals = {
        key: costs[key] - sum((item["costs"][key] for item in per_strategy.values()), ZERO)
        for key in costs
    }
    commission_exact = all(
        sum((item["amount"] for item in event.get("commission_allocations", ())), ZERO)
        == event["commission"]
        for event in orders
        if event["status"] == "filled"
    )
    reconciled = (
        abs(residual) <= tolerance
        and all(abs(value) <= tolerance for value in cost_residuals.values())
        and commission_exact
    )
    if status == "complete" and not reconciled:
        status, reason = "blocked", "accounting_reconciliation_failed"
    result = encode(
        {
            "simulator_version": VERSION,
            "status": status,
            "reason": reason,
            "mode": "synthetic",
            "historical_market_validity": False,
            "execution_enabled": False,
            "input": source.model_dump(mode="json"),
            "input_hash": digest(canonical(source.model_dump(mode="json")).encode()),
            "orders": orders,
            "ledger": ledger,
            "trades": trades,
            "curve": curve,
            "plans": plans,
            "opening_checks": opening_checks,
            "opening_halts": opening_halts,
            "planning_rejections": planning_rejections,
            "positions": [vars(lot) | {"mark": marks[lot.code]} for lot in lots],
            "costs": costs,
            "per_strategy": per_strategy,
            "accounting_diagnostics": {
                "equity_residual": residual,
                "cost_residuals": cost_residuals,
                "tolerance": tolerance,
                "commission_allocations_exact": commission_exact,
            },
            "metrics": {
                "initial_cash": source.initial_cash,
                "final_cash": cash,
                "final_equity": final_equity,
                "total_return": final_equity / source.initial_cash - 1,
                "all_positions_flat": not lots,
                "accounting_reconciled": reconciled,
                "strategy_total_net_pnl": net_pnl,
                "pending_exit_quantity": sum(
                    event["quantity"]
                    for event in orders
                    if event["kind"] == "exit" and event["status"] == "unfilled"
                ),
            }
            if status == "complete"
            else None,
            "assumptions": {
                "start": "flat_cash_no_external_flows",
                "actions": "unsupported_rejected",
                "entries": "fixed_planned_quantity_next_open_all_or_none_two_phase_risk",
                "exits": "strategy_horizon_next_open_retry_without_releasing_failed_lots",
                "short_collateral": "one_plus_additional_ratio_times_current_mark",
                "old_backtest_collateral_unchanged": (
                    "original_proceeds_plus_additional_ratio_times_current_mark"
                ),
                "cash_baselines": "previous_completed_close_equity_and_running_close_peak",
                "fee_allocation": "quantity_pro_rata_last_contributor_exact_remainder",
                "end_of_data": "known_calendar_tail_guard_no_forced_liquidation",
            },
        }
    )
    result["result_hash"] = digest(canonical(result).encode())
    return result
