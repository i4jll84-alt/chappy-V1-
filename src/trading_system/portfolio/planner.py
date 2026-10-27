"""Deterministic fixed-budget sizing, lot netting and reject-whole-code risk checks."""

from collections import defaultdict
from decimal import ROUND_FLOOR, Decimal, localcontext
from itertools import combinations

from trading_system.backtest.engine import encode
from trading_system.portfolio.models import PlanInput
from trading_system.storage.store import canonical, digest

ZERO = Decimal(0)
VERSION = "synthetic_fixed_weight_risk_v1"


def plan_portfolio(request: PlanInput | dict) -> dict:
    request = PlanInput.model_validate(request)
    with localcontext() as context:
        context.prec = 38
        return _plan(request)


def _plan(request):
    instruments = {item.code: item for item in request.instruments}
    strategies = {item.strategy: item for item in request.strategies}

    def priority(name):
        return strategies[name].priority, name

    correlations = {
        tuple(sorted((item.code_a, item.code_b))): item for item in request.correlations
    }
    decision, limits = request.decision_at, request.limits
    proposals, rejections, netting = [], [], []
    account = request.account

    def evidence_error(code):
        instrument = instruments.get(code)
        if instrument is None:
            return "instrument_evidence_missing"
        if not instrument.mark_as_of <= instrument.mark_known_at <= decision:
            return "mark_evidence_unavailable"
        if (decision - instrument.mark_as_of).total_seconds() > limits.max_evidence_age_seconds:
            return "mark_evidence_stale"
        if not (
            instrument.rule_known_at <= decision
            and instrument.valid_from <= decision < instrument.valid_until
        ):
            return "rule_sector_inventory_evidence_unavailable"
        return None

    def risk_price(code, declared_price):
        return max(instruments[code].mark_price, declared_price)

    def fee(code, quantity):
        instrument = instruments[code]
        return instrument.fee_fixed + instrument.fee_per_share * quantity

    def exposure(item, origin, price, fee_buffer=ZERO):
        return {
            "code": item.code,
            "direction": item.direction,
            "quantity": item.quantity,
            "allocations": [entry.model_dump() for entry in item.allocations],
            "origin": origin,
            "price": price,
            "fee_buffer": fee_buffer,
        }

    existing = []
    book_errors = []
    if not account.as_of <= account.known_at <= decision:
        book_errors.append("account_evidence_unavailable")
    elif (decision - account.as_of).total_seconds() > limits.max_evidence_age_seconds:
        book_errors.append("account_evidence_stale")
    for item in (*request.positions, *request.reservations):
        error = evidence_error(item.code)
        if error:
            book_errors.append(error + ":" + item.code)
            continue
        if item.quantity % instruments[item.code].lot_size:
            book_errors.append("existing_quantity_not_trading_unit:" + item.code)
        if any(entry.strategy not in strategies for entry in item.allocations):
            book_errors.append("unknown_exposure_strategy:" + item.code)
    for item in request.reservations:
        if item.known_at > decision:
            book_errors.append("reservation_not_yet_known:" + item.order_id)
        if item.code in instruments:
            if item.fee_buffer < fee(item.code, item.quantity):
                book_errors.append("reservation_fee_buffer_insufficient:" + item.order_id)
            if item.direction < 0 and not instruments[item.code].short_allowed:
                book_errors.append("reserved_short_not_allowed:" + item.order_id)

    def finish(status, before, after, errors):
        result = encode(
            {
                "planner_version": VERSION,
                "mode": "synthetic",
                "status": status,
                "execution_enabled": False,
                "historical_market_validity": False,
                "input": request.model_dump(mode="json"),
                "input_hash": digest(canonical(request.model_dump(mode="json")).encode()),
                "source_result_hashes": sorted(
                    {signal.source_result_hash for signal in request.signals}
                ),
                "proposals": proposals,
                "rejections": rejections,
                "netting": netting,
                "before": before,
                "after": after,
                "blocked_reasons": errors,
                "assumptions": {
                    "sizing": "fixed_strategy_remaining_gross_budget_equal_per_nonzero_signal",
                    "confidence_sizes_quantity": False,
                    "netting": "cancel_lower_priority_attribution_first_per_direction",
                    "risk_failure": "reject_whole_code_no_redistribution",
                    "new_exposures_only": True,
                    "risk_price": "max_dated_mark_and_explicit_planning_price",
                    "cash": "includes_only_existing_short_proceeds_no_hypothetical_sale_credit",
                    "short_ratio": "additional_own_collateral_existing_reserve_one_plus_ratio",
                    "correlation": "absolute_pair_threshold_no_hedge_or_covariance_claim",
                    "stress": "declared_scenario_estimate_not_maximum_loss_or_stop_guarantee",
                    "reservations": "all_declared_open_entries_remain_reserved_without_netting",
                },
            }
        )
        result["result_hash"] = digest(canonical(result).encode())
        return result

    if book_errors:
        return finish("blocked", None, None, sorted(set(book_errors)))
    for item in request.positions:
        existing.append(exposure(item, "position", instruments[item.code].mark_price))
    for item in request.reservations:
        existing.append(
            exposure(
                item, "reservation", risk_price(item.code, item.planning_price), item.fee_buffer
            )
        )
    equity = account.cash + sum(
        (
            item.direction * item.quantity * instruments[item.code].mark_price
            for item in request.positions
        ),
        ZERO,
    )
    if equity <= 0:
        return finish("blocked", None, None, ["nonpositive_account_equity"])

    def metrics(exposures):
        symbol, sector, strategy = (
            defaultdict(lambda: ZERO),
            defaultdict(lambda: ZERO),
            defaultdict(lambda: ZERO),
        )
        short_quantity = defaultdict(int)
        long_gross = short_gross = restricted = reserved_charge = proposed_charge = stress = ZERO
        for item in exposures:
            code, quantity, price = item["code"], item["quantity"], item["price"]
            instrument = instruments[code]
            notional = quantity * price
            symbol[code] += notional
            sector[instrument.sector] += notional
            for allocation in item["allocations"]:
                strategy[allocation["strategy"]] += allocation["quantity"] * price
            if item["direction"] > 0:
                long_gross += notional
            else:
                short_gross += notional
                short_quantity[code] += quantity
            stress += notional * instrument.adverse_move_fraction + item["fee_buffer"]
            if item["origin"] == "position":
                if item["direction"] < 0:
                    restricted += (1 + instrument.short_additional_collateral_ratio) * notional
            else:
                charge = (
                    notional
                    if item["direction"] > 0
                    else instrument.short_additional_collateral_ratio * notional
                )
                charge += item["fee_buffer"]
                if item["origin"] == "reservation":
                    reserved_charge += charge
                else:
                    proposed_charge += charge
        available = account.cash - restricted - reserved_charge - proposed_charge
        return {
            "equity": equity,
            "account_cash": account.cash,
            "existing_short_restricted_cash": restricted,
            "reservation_cash_charge": reserved_charge,
            "proposal_cash_charge": proposed_charge,
            "available_cash": available,
            "available_cash_ratio": available / equity,
            "minimum_required_cash": equity * limits.min_cash_ratio,
            "long_gross": long_gross,
            "short_gross": short_gross,
            "total_gross": long_gross + short_gross,
            "symbol_gross": dict(sorted(symbol.items())),
            "sector_gross": dict(sorted(sector.items())),
            "strategy_gross": dict(sorted(strategy.items())),
            "short_quantities": dict(sorted(short_quantity.items())),
            "daily_loss_fraction": max(ZERO, 1 - equity / account.day_start_equity),
            "reference_loss_fraction": max(ZERO, 1 - equity / account.reference_equity),
            "stress_loss_estimate": stress,
            "stress_loss_fraction": stress / equity,
        }

    def risk_errors(state):
        errors = []
        for code, gross in state["symbol_gross"].items():
            if gross > equity * limits.max_symbol_fraction:
                errors.append("symbol_cap:" + code)
        for sector, gross in state["sector_gross"].items():
            if gross > equity * limits.max_sector_fraction:
                errors.append("sector_cap:" + sector)
        for strategy, gross in state["strategy_gross"].items():
            if gross > equity * strategies[strategy].weight:
                errors.append("strategy_budget:" + strategy)
        for key, cap, reason in (
            ("long_gross", equity * limits.max_long_fraction, "long_gross_cap"),
            ("short_gross", equity * limits.max_short_fraction, "short_gross_cap"),
            ("total_gross", equity * limits.max_gross_fraction, "total_gross_cap"),
            ("short_gross", limits.short_credit_limit, "short_credit_limit"),
            ("stress_loss_estimate", equity * limits.max_stress_loss_fraction, "stress_loss_cap"),
        ):
            if state[key] > cap:
                errors.append(reason)
        if state["available_cash"] < state["minimum_required_cash"]:
            errors.append("minimum_cash_ratio")
        for code, quantity in state["short_quantities"].items():
            if quantity > instruments[code].short_total_capacity:
                errors.append("short_total_inventory:" + code)
        for left, right in combinations(state["symbol_gross"], 2):
            pair = correlations.get((left, right))
            suffix = ":" + left + ":" + right
            if pair is None:
                errors.append("correlation_evidence_missing" + suffix)
            elif not pair.window_end <= pair.known_at <= decision or pair.valid_until <= decision:
                errors.append("correlation_evidence_unavailable" + suffix)
            elif abs(pair.correlation) >= limits.correlation_threshold and (
                state["symbol_gross"][left] + state["symbol_gross"][right]
                > equity * limits.correlated_pair_cap
            ):
                errors.append("correlated_pair_cap" + suffix)
        return sorted(errors)

    before = metrics(existing)
    errors = risk_errors(before)
    if equity <= account.day_start_equity * (1 - limits.max_daily_loss_fraction):
        errors.append("daily_loss_halt")
    if equity <= account.reference_equity * (1 - limits.max_reference_loss_fraction):
        errors.append("reference_loss_halt")
    if errors:
        return finish("blocked", before, before, sorted(set(errors)))
    contributions = defaultdict(list)
    grouped = defaultdict(list)
    for signal in request.signals:
        if signal.strategy not in strategies:
            rejections.append(
                {
                    "code": signal.code,
                    "strategy": signal.strategy,
                    "reason": "unknown_signal_strategy",
                }
            )
            continue
        if not (
            signal.decision_at == decision
            and signal.trained_at <= signal.known_at <= decision
            and signal.feature_known_at <= signal.known_at
        ):
            rejections.append(
                {
                    "code": signal.code,
                    "strategy": signal.strategy,
                    "reason": "signal_temporal_evidence_unavailable",
                }
            )
            continue
        if signal.direction == 0:
            rejections.append(
                {"code": signal.code, "strategy": signal.strategy, "reason": "zero_direction"}
            )
            continue
        grouped[signal.strategy].append(signal)
    for strategy in sorted(grouped, key=priority):
        remaining = max(
            ZERO,
            equity * strategies[strategy].weight - before["strategy_gross"].get(strategy, ZERO),
        )
        per_code_budget = remaining / len(grouped[strategy])
        for signal in sorted(grouped[strategy], key=lambda item: item.code):
            error = evidence_error(signal.code)
            if error:
                rejections.append({"code": signal.code, "strategy": strategy, "reason": error})
                continue
            instrument = instruments[signal.code]
            price = risk_price(signal.code, instrument.planning_price)
            quantity = (
                int(
                    (per_code_budget / (price * instrument.lot_size)).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
                * instrument.lot_size
            )
            if quantity == 0:
                rejections.append(
                    {
                        "code": signal.code,
                        "strategy": strategy,
                        "reason": "remaining_budget_below_one_lot",
                    }
                )
                continue
            contributions[signal.code].append(
                {
                    "strategy": strategy,
                    "direction": signal.direction,
                    "quantity": quantity,
                    "model_id": signal.model_id,
                    "source_result_hash": signal.source_result_hash,
                }
            )
    merged = []
    for code in sorted(contributions):
        items = sorted(contributions[code], key=lambda item: priority(item["strategy"]))
        totals = {
            direction: sum(item["quantity"] for item in items if item["direction"] == direction)
            for direction in (1, -1)
        }
        cancellation = min(totals.values())
        cancelled = []
        for direction in (1, -1):
            remaining = cancellation
            for item in reversed(items):
                if item["direction"] != direction:
                    continue
                removed = min(item["quantity"], remaining)
                item["quantity"] -= removed
                remaining -= removed
                if removed:
                    cancelled.append(
                        {"strategy": item["strategy"], "direction": direction, "quantity": removed}
                    )
        survivors = [item for item in items if item["quantity"]]
        netting.append(
            {
                "code": code,
                "long_quantity": totals[1],
                "short_quantity": totals[-1],
                "cancelled": cancelled,
                "net_quantity": sum(item["quantity"] for item in survivors),
            }
        )
        if not survivors:
            rejections.append({"code": code, "reason": "opposite_contributions_fully_netted"})
            continue
        direction = survivors[0]["direction"]
        merged.append(
            {
                "code": code,
                "direction": direction,
                "quantity": sum(item["quantity"] for item in survivors),
                "contributors": survivors,
            }
        )
    accepted = []
    occupied = {item["code"] for item in existing}
    for proposal in sorted(
        merged,
        key=lambda item: (min(priority(c["strategy"]) for c in item["contributors"]), item["code"]),
    ):
        code, quantity = proposal["code"], proposal["quantity"]
        instrument = instruments[code]
        if code in occupied:
            opposite = any(
                item["code"] == code and item["direction"] != proposal["direction"]
                for item in existing
            )
            rejections.append(
                {
                    "code": code,
                    "reason": "unsupported_existing_reversal"
                    if opposite
                    else "duplicate_existing_or_reserved_code",
                }
            )
            continue
        if proposal["direction"] < 0 and not instrument.short_allowed:
            rejections.append({"code": code, "reason": "new_short_not_allowed"})
            continue
        price, buffer = risk_price(code, instrument.planning_price), fee(code, quantity)
        new_exposure = {
            "code": code,
            "direction": proposal["direction"],
            "quantity": quantity,
            "allocations": [
                {"strategy": c["strategy"], "quantity": c["quantity"]}
                for c in proposal["contributors"]
            ],
            "origin": "proposal",
            "price": price,
            "fee_buffer": buffer,
        }
        proposed = metrics(existing + accepted + [new_exposure])
        reasons = risk_errors(proposed)
        if reasons:
            rejections.append({"code": code, "reason": "proposal_risk_limit", "details": reasons})
            continue
        accepted.append(new_exposure)
        proposals.append(
            proposal
            | {
                "planning_risk_price": price,
                "planning_notional": price * quantity,
                "fee_buffer": buffer,
                "resource_cash_charge": quantity
                * price
                * (1 if proposal["direction"] > 0 else instrument.short_additional_collateral_ratio)
                + buffer,
            }
        )
    return finish("complete", before, metrics(existing + accepted), [])
