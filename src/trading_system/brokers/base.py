"""One contract for adapters, independent of Windows, Excel, and API credentials."""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from trading_system.brokers.models import (
    Account,
    Balance,
    Board,
    Connection,
    Costs,
    Credit,
    Fill,
    Order,
    OrderRequest,
    Position,
    Quote,
    ShortAvailability,
)


class Unavailable(RuntimeError):
    def __init__(self, feature, reason):
        self.feature, self.reason = str(feature), reason
        super().__init__(f"{self.feature}: {reason}")


class BrokerAdapter(ABC):
    account: Account
    real_execution_enabled = False

    @abstractmethod
    def capabilities(self): ...

    @abstractmethod
    def connect(self) -> Connection: ...

    @abstractmethod
    def connection_status(self) -> Connection: ...

    @abstractmethod
    def quote(self, code: str, now: datetime) -> Quote: ...

    @abstractmethod
    def board(self, code: str, now: datetime) -> Board: ...

    @abstractmethod
    def submit(self, request: OrderRequest, now: datetime) -> Order: ...

    @abstractmethod
    def order(self, internal_id: str, now: datetime) -> Order | None: ...

    @abstractmethod
    def orders(self, now: datetime) -> tuple[Order, ...]: ...

    @abstractmethod
    def cancel(self, internal_id: str, now: datetime) -> Order: ...

    @abstractmethod
    def modify(self, internal_id: str, price: Decimal, quantity: int, now: datetime) -> Order: ...

    @abstractmethod
    def fills(self, now: datetime) -> tuple[Fill, ...]: ...

    @abstractmethod
    def positions(self, now: datetime) -> tuple[Position, ...]: ...

    @abstractmethod
    def balance(self, now: datetime) -> Balance: ...

    @abstractmethod
    def short_availability(self, code: str, credit: Credit, now: datetime) -> ShortAvailability: ...

    @abstractmethod
    def costs(self, code: str, now: datetime) -> Costs: ...

    @abstractmethod
    def validate(self, request: OrderRequest, now: datetime): ...

    @abstractmethod
    def reconcile_account(self, now: datetime) -> bool: ...

    @abstractmethod
    def emergency_stop(self): ...

    @abstractmethod
    def close(self): ...


class DisconnectedAdapter(BrokerAdapter):
    """Fail-closed skeleton. No network, COM, subprocess, credentials or fallback."""

    broker = None

    def __init__(self, account, *, enabled=False):
        if account.broker != self.broker:
            raise ValueError("broker_identity_mismatch")
        self.account, self.enabled = account, enabled

    def capabilities(self):
        from trading_system.brokers.capabilities import capabilities

        return capabilities(self.broker)

    def _unavailable(self, feature):
        raise Unavailable(feature, "not_implemented" if self.enabled else "disabled")

    def connect(self):
        self._unavailable("connect")

    def connection_status(self):
        return Connection(
            account=self.account,
            state="not_implemented" if self.enabled else "disabled",
        )

    def quote(self, code, now):
        self._unavailable("quote")

    def board(self, code, now):
        self._unavailable("board")

    def submit(self, request, now):
        self._unavailable("submit")

    def order(self, internal_id, now):
        self._unavailable("orders")

    def orders(self, now):
        self._unavailable("orders")

    def cancel(self, internal_id, now):
        self._unavailable("cancel")

    def modify(self, internal_id, price, quantity, now):
        self._unavailable("modify")

    def fills(self, now):
        self._unavailable("fills")

    def positions(self, now):
        self._unavailable("positions")

    def balance(self, now):
        self._unavailable("balance")

    def short_availability(self, code, credit, now):
        self._unavailable("short_availability")

    def costs(self, code, now):
        self._unavailable("costs")

    def validate(self, request, now):
        self._unavailable("validate")

    def reconcile_account(self, now):
        self._unavailable("reconcile_account")

    def emergency_stop(self):
        # No transport or orders exist in a disconnected skeleton.
        self.enabled = False

    def close(self):
        pass
