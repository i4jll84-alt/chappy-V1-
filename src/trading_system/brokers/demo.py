"""Fixed synthetic contract scenario; no live quotes or credentials required."""

from datetime import timedelta

from trading_system.brokers.models import (
    Board,
    Level,
    OrderRequest,
    PaperMarket,
    Quote,
    SessionWindow,
    ShortAvailability,
)
from trading_system.brokers.registry import PaperFixture
from trading_system.execution.demo import NOW, mock_config


def fixture():
    return PaperFixture(
        risk=mock_config(),
        market=PaperMarket(
            sessions=(SessionWindow(opens_at=NOW, closes_at=NOW + timedelta(hours=1)),),
            max_age_seconds=3600,
            quotes=(Quote(code="MOCK-A", price="100", known_at=NOW, evidence="simulated"),),
            boards=(
                Board(
                    code="MOCK-A",
                    bids=(Level(price="99", quantity=1000),),
                    asks=(Level(price="101", quantity=1000),),
                    known_at=NOW,
                    evidence="simulated",
                ),
            ),
            shorts=tuple(
                ShortAvailability(
                    code="MOCK-A",
                    credit=credit,
                    allowed=True,
                    quantity=1000,
                    known_at=NOW,
                    valid_until=NOW + timedelta(hours=1),
                    evidence="simulated",
                )
                for credit in ("general", "standard")
            ),
        ),
    )


def request(account, internal_id, action="cash_buy", quantity=100, **kwargs):
    return OrderRequest(
        account=account,
        internal_id=internal_id,
        code="MOCK-A",
        action=action,
        quantity=quantity,
        limit_price="100",
        decision_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        strategy="fixed_contract_demo",
        source_hash="a" * 64,
        **kwargs,
    )


def run_demo(router):
    """Accept, partially fill, reconcile, fill remainder, close; repeat is query-only."""
    for key, action in (("phase1-demo-buy", "cash_buy"), ("phase1-demo-sell", "cash_sell")):
        order = router.submit(request(router.active, key, action), NOW)
        while order.filled_quantity < 100:
            quantity = 40 if order.filled_quantity == 0 else 60
            order = router.adapter.simulate_fill(
                key,
                quantity,
                "100",
                f"{key}-{order.filled_quantity}",
                NOW,
            )
    return {
        "mode": "paper",
        "real_execution_enabled": False,
        "historical_market_validity": False,
        "scenario": "fixed_synthetic_contract",
        "account": router.active.model_dump(mode="json"),
        "orders": [o.model_dump(mode="json") for o in router.adapter.orders(NOW)],
        "balance": router.adapter.balance(NOW).model_dump(mode="json"),
        "positions": [p.model_dump(mode="json") for p in router.adapter.positions(NOW)],
        "accounting_matches": all(router.reconcile(NOW).values()),
    }
