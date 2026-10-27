"""Durable route pinning and explicit flat-account switching, paper only."""

import sqlite3
from contextlib import ExitStack

from trading_system.brokers.base import Unavailable
from trading_system.brokers.locking import single_writer
from trading_system.brokers.models import Account, OrderRequest
from trading_system.brokers.registry import Config, PaperFixture, build_paper
from trading_system.execution.risk import TERMINAL
from trading_system.execution.store import digest
from trading_system.storage.store import canonical


class BrokerRouter:
    def __init__(self, root, config, fixture):
        config, fixture = Config.model_validate(config), PaperFixture.model_validate(fixture)
        self.root, self.config, self.fixture = root, config, fixture
        self.adapters = {}
        self.operation_guard = None
        self._resources = ExitStack()
        try:
            self._resources.enter_context(single_writer(root))
            self.db = sqlite3.connect(root / "routing.sqlite")
            self._resources.callback(self.db.close)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS routes(
                    internal_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, account TEXT NOT NULL
                );
            """)
            selected = config.account(config.trading.broker)
            row = self.db.execute("SELECT value FROM metadata WHERE key='active'").fetchone()
            if row is None:
                self._set_active(selected)
            else:
                self.active = Account.model_validate_json(row[0])
                if self.active != selected:
                    raise ValueError("persisted_route_differs_use_explicit_switch")
            self._adapter(self.active)
        except BaseException:
            self._resources.close()
            raise

    def _adapter(self, account):
        if self.config.account(account.broker) != account:
            raise ValueError("account_binding_changed")
        key = digest(account.model_dump(mode="json"))
        if key not in self.adapters:
            adapter = build_paper(self.root, account, self.fixture)
            self._resources.callback(adapter.close)
            adapter.connect()
            self.adapters[key] = adapter
        return self.adapters[key]

    @property
    def adapter(self):
        return self._adapter(self.active)

    def _set_active(self, account):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('active',?)",
                (canonical(account.model_dump(mode="json")),),
            )
        self.active = account

    def submit(self, request, now):
        request = OrderRequest.model_validate(request)
        if self.operation_guard:
            self.operation_guard("submit", request)
        fingerprint = digest(request.model_dump(mode="json"))
        row = self.db.execute(
            "SELECT fingerprint, account FROM routes WHERE internal_id=?", (request.internal_id,)
        ).fetchone()
        if row:
            if row[0] != fingerprint:
                raise ValueError("immutable_order_route_or_content")
            result = self._adapter(Account.model_validate_json(row[1])).order(
                request.internal_id, now
            )
            if result is None:
                raise Unavailable("submit", "previous_outcome_unknown_do_not_resend")
            return result
        if request.account != self.active:
            raise ValueError("order_must_target_selected_account")
        halted = self.db.execute("SELECT value FROM metadata WHERE key='halt'").fetchone()
        if halted and request.action in {"cash_buy", "margin_short"}:
            raise ValueError("new_entries_halted")
        # Validate before reserving a route; after the durable claim, never auto retry.
        self._require_known_routes(self.adapter, now)
        self.adapter.validate(request, now)
        with self.db:
            self.db.execute(
                "INSERT INTO routes VALUES(?,?,?)",
                (
                    request.internal_id,
                    fingerprint,
                    canonical(request.account.model_dump(mode="json")),
                ),
            )
        return self.adapter.submit(request, now)

    def switch(self, broker, now):
        if self.operation_guard:
            self.operation_guard("switch")
        target = self.config.account(broker)  # Disabled targets are never instantiated.
        old, new = self.adapter, self._adapter(target)
        # Query BOTH sides, including held positions, even when orders block switching.
        snapshots = [(a.orders(now), a.positions(now)) for a in (old, new)]
        if any(o.state not in TERMINAL for orders, _ in snapshots for o in orders):
            raise ValueError("switch_blocked_by_open_or_unknown_orders")
        if any(positions for _, positions in snapshots):
            raise ValueError("switch_requires_flat_accounts")
        for adapter in (old, new):
            self._require_known_routes(adapter, now)
        self._set_active(target)

    def _require_known_routes(self, adapter, now):
        bound = canonical(adapter.account.model_dump(mode="json"))
        rows = self.db.execute("SELECT internal_id FROM routes WHERE account=?", (bound,))
        for (internal_id,) in rows.fetchall():
            if adapter.order(internal_id, now) is None:
                raise Unavailable("route", "unresolved_submission_requires_review")

    def _order_adapter(self, internal_id):
        row = self.db.execute(
            "SELECT account FROM routes WHERE internal_id=?", (internal_id,)
        ).fetchone()
        if row is None:
            raise Unavailable("order", "unknown_internal_id")
        return self._adapter(Account.model_validate_json(row[0]))

    def order(self, internal_id, now):
        return self._order_adapter(internal_id).order(internal_id, now)

    def cancel(self, internal_id, now):
        if self.operation_guard:
            self.operation_guard("cancel")
        adapter = self._order_adapter(internal_id)
        existing = adapter.order(internal_id, now)
        if existing is not None and existing.state in TERMINAL:
            return existing
        return adapter.cancel(internal_id, now)

    def modify(self, internal_id, price, quantity, now):
        if self.operation_guard:
            self.operation_guard("modify")
        return self._order_adapter(internal_id).modify(internal_id, price, quantity, now)

    def reconcile(self, now):
        result = {}
        for adapter in self.adapters.values():
            adapter.orders(now)
            result[adapter.account.broker] = adapter.reconcile_account(now)
        return result

    def emergency_stop(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES('halt','true')")
        for adapter in self.adapters.values():
            adapter.emergency_stop()
        # No automatic cancellation or liquidation.

    def close(self):
        self._resources.close()
