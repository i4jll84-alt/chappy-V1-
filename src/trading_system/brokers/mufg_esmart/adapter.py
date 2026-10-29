"""Optional market-data-only implementation of the existing broker contract."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from trading_system.brokers.base import DisconnectedAdapter, Unavailable
from trading_system.brokers.capabilities import Feature
from trading_system.brokers.models import Board, Connection, Level, Quote
from trading_system.brokers.mufg_esmart.connection_check import _object, inspect_board
from trading_system.point_in_time.policy import aware_utc
from trading_system.runtime.models import JST


class Adapter(DisconnectedAdapter):
    broker = "mufg_esmart"

    def __init__(self, account, *, enabled=False, market_data_client=None, clock=None):
        super().__init__(account, enabled=enabled)
        if market_data_client is not None and not enabled:
            raise ValueError("market_data_disabled")
        self.client = market_data_client
        self.clock = clock or (lambda: datetime.now(UTC))
        self.state = "offline"
        self.max_age_seconds = 60
        self._good = 0
        self._quote = self._board = None
        self._last_quote_time = None
        self.last_observation = None

    def capabilities(self):
        result = super().capabilities()
        if self.client is not None and self.enabled:
            for feature in (Feature.CONNECT, Feature.CONNECTION, Feature.QUOTE, Feature.BOARD):
                result[feature] = result[feature].model_copy(
                    update={
                        "vendor_support": "documented",
                        "implementation": "read_only",
                        "verification": "per_session_evidence_required",
                        "reason": "Fixed 7203 board; no trading or account transport",
                    }
                )
        return result

    def _require_client(self):
        if not self.enabled or self.client is None:
            self._unavailable("connect")

    def connect(self):
        self.refresh()
        return self.connection_status()

    def connection_status(self):
        if self.client is None or not self.enabled:
            return super().connection_status()
        return Connection(
            account=self.account,
            state=self.state,
            market_data_verified=self.state == "read_only_ready",
        )

    def refresh(self):
        self._require_client()
        self._quote = self._board = None
        record, raw = self.client.read()
        record.update(issues=[], common_quote=None, common_board=None, accepted=False)
        if record["outcome"] != "ok":
            self._good = 0
            self.state = "reconnecting" if record["retryable"] else record["outcome"]
        elif record["operation"] == "token":
            self._good = 0
            self.state = "connecting"
        else:
            completed = aware_utc(datetime.fromisoformat(record["read_completed_at"]))
            data = _object(raw)
            issues = inspect_board(data, completed)["issues"]
            try:
                quote_time = aware_utc(datetime.fromisoformat(data["CurrentPriceTime"]))
                if (
                    quote_time.astimezone(JST).date() != completed.astimezone(JST).date()
                    or not 0 <= (completed - quote_time).total_seconds() <= self.max_age_seconds
                ):
                    issues.append("quote_stale_or_future")
                if self._last_quote_time and quote_time < self._last_quote_time:
                    issues.append("quote_time_regressed")
                bids, asks = [], []
                for side, levels in (("Buy", bids), ("Sell", asks)):
                    for index in range(1, 11):
                        level = data[f"{side}{index}"]
                        qty = level["Qty"]
                        if isinstance(qty, bool) or not isinstance(qty, (int, float)):
                            raise ValueError
                        if qty <= 0 or int(qty) != qty:
                            raise ValueError
                        levels.append(Level(price=Decimal(str(level["Price"])), quantity=int(qty)))
                if (
                    any(a.price < b.price for a, b in zip(bids, bids[1:]))
                    or any(a.price > b.price for a, b in zip(asks, asks[1:]))
                    or bids[0].price >= asks[0].price
                ):
                    issues.append("board_order_or_crossing")
                quote = Quote(
                    code="7203",
                    price=Decimal(str(data["CurrentPrice"])),
                    known_at=completed,
                    source_updated_at=quote_time,
                    evidence="observed",
                    freshness_verified=False,
                )
                board = Board(
                    code="7203",
                    bids=tuple(bids),
                    asks=tuple(asks),
                    known_at=completed,
                    evidence="observed",
                )
            except (
                ValueError,
                TypeError,
                KeyError,
                OverflowError,
                InvalidOperation,
                ValidationError,
            ):
                issues.append("common_market_data_invalid")
            if datetime.fromisoformat(record["read_started_at"]) > completed:
                issues.append("observation_clock_regressed")
            record["issues"] = sorted(set(issues))
            if issues:
                self._good = 0
                self.state = "waiting_for_data"
            else:
                # Keep the high-water mark across gaps, using only validated observations.
                self._last_quote_time = max(quote_time, self._last_quote_time or quote_time)
                self._good += 1
                self.state = "read_only_ready" if self._good >= 3 else "waiting_for_data"
                record.update(
                    common_quote=quote.model_dump(mode="json"),
                    common_board=board.model_dump(mode="json"),
                )
                if self._good >= 3:
                    self._quote, self._board = quote, board
                    record["accepted"] = True
        record["connection_state"] = self.state
        self.last_observation = (record, raw)
        return self.last_observation

    def _cached(self, code, now, feature):
        self._require_client()
        now = aware_utc(now)
        quote = self._quote
        if (
            code != "7203"
            or self.state != "read_only_ready"
            or quote is None
            or not 0 <= (now - quote.known_at).total_seconds() <= self.max_age_seconds
            or not 0 <= (now - quote.source_updated_at).total_seconds() <= self.max_age_seconds
            or now.astimezone(JST).date() != quote.known_at.astimezone(JST).date()
        ):
            raise Unavailable(feature, "market_data_not_ready")
        return quote if feature == "quote" else self._board

    def quote(self, code, now):
        if self.client is None:
            return super().quote(code, now)
        return self._cached(code, now, "quote")

    def board(self, code, now):
        if self.client is None:
            return super().board(code, now)
        return self._cached(code, now, "board")

    def emergency_stop(self):
        self.close()

    def close(self):
        self._quote = self._board = None
        self._good = 0
        self.state = "stopped"
        if self.client is not None:
            self.client.close()
