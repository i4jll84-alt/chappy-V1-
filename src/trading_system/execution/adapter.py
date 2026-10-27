"""Convert verified synthetic proposals to deterministic, strictly mock intents."""

from datetime import datetime
from decimal import Decimal

from trading_system.execution.models import OrderIntent
from trading_system.point_in_time.policy import aware_utc
from trading_system.portfolio.planner import plan_portfolio
from trading_system.storage.store import canonical, digest


def intents_from_plan(
    result: dict, *, limit_prices: dict[str, Decimal], expires_at: datetime
) -> tuple[OrderIntent, ...]:
    """Require reproducible planner output and explicit mock limit prices.

    A content hash detects corruption, not identity or trading authorization.
    The same plan/code/side has one client id regardless of caller price changes;
    the durable service must reject changed content under that id.
    """
    expires_at = aware_utc(expires_at)
    if (
        result.get("mode") != "synthetic"
        or result.get("status") != "complete"
        or result.get("execution_enabled") is not False
        or result.get("historical_market_validity") is not False
    ):
        raise ValueError("Only complete synthetic planner output is accepted")
    content = {key: value for key, value in result.items() if key != "result_hash"}
    if digest(canonical(content).encode()) != result.get("result_hash"):
        raise ValueError("Planner result hash mismatch")
    expected = plan_portfolio(result["input"])
    if expected != result:
        raise ValueError("Planner output does not reproduce from saved input")
    decision_at = aware_utc(datetime.fromisoformat(result["input"]["decision_at"]))
    if expires_at <= decision_at:
        raise ValueError("Mock order expiration must follow the planning decision")
    proposals = result["proposals"]
    if set(limit_prices) != {proposal["code"] for proposal in proposals}:
        raise ValueError("Explicit limit prices must exactly cover accepted proposals")
    intents = []
    for proposal in sorted(proposals, key=lambda item: item["code"]):
        code = proposal["code"]
        price = limit_prices[code]
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise ValueError("Mock limit prices require finite positive Decimal values")
        side = "buy" if proposal["direction"] > 0 else "short"
        client_id = digest(
            canonical({"source": result["result_hash"], "code": code, "side": side}).encode()
        )
        names = {item["strategy"] for item in proposal["contributors"]}
        intents.append(
            OrderIntent(
                client_id=client_id,
                code=code,
                side=side,
                quantity=proposal["quantity"],
                limit_price=price,
                decision_at=decision_at,
                expires_at=expires_at,
                strategy=next(iter(names)) if len(names) == 1 else "portfolio",
                source_hash=result["result_hash"],
            )
        )
    return tuple(intents)
