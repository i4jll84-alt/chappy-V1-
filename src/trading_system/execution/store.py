"""SQLite transactions for mock-only intent, account and append-only events."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from trading_system.storage.store import canonical


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class StateStore:
    def __init__(self, path: Path, config):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(client_id TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,value TEXT NOT NULL);
        """)
        payload = config.model_dump(mode="json")
        with self.transaction():
            existing = self.get_meta("config")
            if existing is not None and existing != payload:
                raise ValueError("Persisted MOCK configuration is immutable")
            if existing is None:
                self.set_meta("config", payload)
                self.set_meta(
                    "account",
                    {"cash": str(config.initial_cash), "positions": config.initial_positions},
                )
                self.set_meta("halt", {"emergency": False, "uncertainty": False})
                self.event("initialized", {"config_hash": digest(payload)})

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, canonical(value)))

    def order(self, client_id):
        row = self.db.execute("SELECT value FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_order(self, order):
        self.db.execute(
            "INSERT OR REPLACE INTO orders VALUES(?,?)", (order["client_id"], canonical(order))
        )

    def orders(self):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT value FROM orders ORDER BY client_id")
        ]

    def event(self, kind, value):
        self.db.execute("INSERT INTO events(kind,value) VALUES(?,?)", (kind, canonical(value)))

    def events(self):
        return [
            {"sequence": r[0], "kind": r[1], "value": json.loads(r[2])}
            for r in self.db.execute("SELECT sequence,kind,value FROM events ORDER BY sequence")
        ]

    def close(self):
        self.db.close()
