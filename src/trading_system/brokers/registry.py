"""Strict paper-only configuration and side-effect-free vendor selection."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from trading_system.brokers.base import Unavailable
from trading_system.brokers.models import Account, Broker, ContractInput, PaperMarket
from trading_system.brokers.paper.adapter import Adapter as PaperAdapter
from trading_system.execution.models import MockConfig
from trading_system.execution.store import digest

BROKERS = ("rakuten", "tachibana", "mufg_esmart")


class BrokerSetting(ContractInput):
    enabled: StrictBool = False
    account_alias: str = Field(default="paper-main", pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class Trading(ContractInput):
    mode: Literal["paper"] = "paper"
    broker: Broker = "rakuten"
    live_trading_enabled: StrictBool = False

    @field_validator("live_trading_enabled")
    @classmethod
    def no_live(cls, value):
        if value:
            raise ValueError("Phase 1 cannot enable live trading")
        return value


class Config(ContractInput):
    trading: Trading = Field(default_factory=Trading)
    brokers: dict[Broker, BrokerSetting] = Field(
        default_factory=lambda: {b: BrokerSetting(enabled=b == "rakuten") for b in BROKERS}
    )

    @model_validator(mode="after")
    def all_brokers(self):
        if set(self.brokers) != set(BROKERS):
            raise ValueError("configure_exactly_three_brokers")
        if not self.brokers[self.trading.broker].enabled:
            raise ValueError("selected_broker_disabled")
        return self

    def account(self, broker):
        entry = self.brokers[broker]
        if not entry.enabled:
            raise Unavailable("broker", "disabled")
        return Account(broker=broker, account_id=entry.account_alias)


class PaperFixture(ContractInput):
    risk: MockConfig
    market: PaperMarket


def load_config(path: Path):
    return Config.model_validate(tomllib.loads(path.read_text(encoding="utf-8-sig")))


def vendor_adapter(account, *, enabled=False):
    # Lazy imports keep platform-specific implementations outside the common core.
    if account.broker == "rakuten":
        from trading_system.brokers.rakuten.adapter import Adapter
    elif account.broker == "tachibana":
        from trading_system.brokers.tachibana.adapter import Adapter
    else:
        from trading_system.brokers.mufg_esmart.adapter import Adapter
    return Adapter(account, enabled=enabled)


def build_paper(root, account, fixture):
    return PaperAdapter(
        root / "accounts" / digest(account.model_dump(mode="json")),
        account,
        fixture.risk,
        fixture.market,
    )
