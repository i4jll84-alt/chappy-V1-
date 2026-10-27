"""Local accounting smoke test fed by RSS observations. No broker or order API."""

import json
import sqlite3
from datetime import datetime, time
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_system.storage.rss import inspect_record
from trading_system.storage.store import canonical

JST = ZoneInfo("Asia/Tokyo")
CONFIG = {
    "initial_cash": "1000000",
    "quantity": 100,
    "fee_per_fill": "10",
    "adverse_slippage_bps": "5",
    "max_age_seconds": 30,
    "scenario": "one_fixed_roundtrip_connectivity_test",
}


class PaperLedger:
    def __init__(self, path: Path, code: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (
            source TEXT PRIMARY KEY, hash TEXT NOT NULL, body TEXT NOT NULL);
        """)
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        if row and (json.loads(row[0])["code"] != code or json.loads(row[0])["config"] != CONFIG):
            self.db.close()
            raise ValueError("paper_configuration_changed")
        with self.db:
            if row is None:
                self._save(
                    dict(
                        code=code,
                        config=CONFIG,
                        cash=CONFIG["initial_cash"],
                        position=0,
                        fills=0,
                        fees="0",
                        phase="buy_signal",
                        last_observed=None,
                        last_quote=None,
                        pending_at=None,
                    )
                )

    def close(self):
        self.db.close()

    def _state(self):
        return json.loads(self.db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])

    def _save(self, state):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (1,?)", (canonical(state),))

    def consume(self, source: str, body: bytes, now: datetime, *, recovery=False):
        digest = sha256(body).hexdigest()
        with self.db:
            previous = self.db.execute(
                "SELECT hash FROM events WHERE source=?", (source,)
            ).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValueError("paper_source_identity_conflict")
                return False
            state = self._state()
            checked = inspect_record(body, now)
            event = dict(
                source=source,
                sha256=digest,
                processed_at=now.isoformat(),
                action="skip",
                reason=None,
                real_execution_enabled=False,
            )
            reason = None
            stamp = None
            quote = None
            if checked["status"] != "observed":
                reason = ",".join(checked["issues"])
            elif checked["code"] != state["code"]:
                reason = "unexpected_code"
            else:
                stamp = datetime.fromisoformat(checked["observed_at"])
                values = checked["normalized"]
                local = stamp.astimezone(JST)
                # This is only a conservative screen, NOT a source-update timestamp.
                seconds = local.hour * 3600 + local.minute * 60 + local.second
                clock = time.fromisoformat(values["quote_time"])
                displayed_seconds = clock.hour * 3600 + clock.minute * 60 + clock.second
                quote = (values["current_date"], values["quote_time"])
                if recovery:
                    reason = "recovery_no_retroactive_fills"
                elif not 0 <= (now - stamp).total_seconds() <= CONFIG["max_age_seconds"]:
                    reason = "old_observation"
                elif not 0 <= seconds - displayed_seconds <= CONFIG["max_age_seconds"]:
                    reason = "display_clock_outside_test_window"
                elif state["last_observed"] and stamp <= datetime.fromisoformat(
                    state["last_observed"]
                ):
                    reason = "non_increasing_observation"
                elif state["last_quote"] and quote <= tuple(state["last_quote"]):
                    reason = "quote_clock_not_advanced"
            # A repeated quote cannot fill. Do not invalidate a pending intent until
            # the source becomes stale or a read fails; its own lifetime is bounded.
            if reason:
                event["reason"] = reason
                if reason != "quote_clock_not_advanced" and state["phase"] != "complete":
                    state["phase"] = "sell_signal" if state["position"] else "buy_signal"
                    state["pending_at"] = None
            else:
                state["last_observed"] = stamp.isoformat()
                state["last_quote"] = quote
                if (
                    state["pending_at"]
                    and (stamp - datetime.fromisoformat(state["pending_at"])).total_seconds()
                    > CONFIG["max_age_seconds"]
                ):
                    state["phase"] = "sell_signal" if state["position"] else "buy_signal"
                    state["pending_at"] = None
                phase = state["phase"]
                if phase in ("buy_signal", "sell_signal"):
                    state["phase"] = phase.replace("signal", "pending")
                    state["pending_at"] = stamp.isoformat()
                    event["action"] = phase
                elif phase in ("buy_pending", "sell_pending"):
                    side = "buy" if phase == "buy_pending" else "sell"
                    price = Decimal(checked["normalized"]["price"])
                    slippage = Decimal(CONFIG["adverse_slippage_bps"]) / 10000
                    fill_price = price * (1 + slippage if side == "buy" else 1 - slippage)
                    quantity = CONFIG["quantity"]
                    fee = Decimal(CONFIG["fee_per_fill"])
                    cash = Decimal(state["cash"])
                    if side == "buy":
                        cost = fill_price * quantity + fee
                        if cash < cost:
                            event["reason"] = "paper_insufficient_cash"
                            state["phase"] = "buy_signal"
                        else:
                            state["cash"] = str(cash - cost)
                            state["position"] = quantity
                            state["phase"] = "sell_signal"
                    else:
                        if state["position"] != quantity:
                            raise ValueError("paper_position_mismatch")
                        state["cash"] = str(cash + fill_price * quantity - fee)
                        state["position"] = 0
                        state["phase"] = "complete"
                    state["pending_at"] = None
                    if event["reason"] is None:
                        state["fills"] += 1
                        state["fees"] = str(Decimal(state["fees"]) + fee)
                        event.update(
                            action="simulated_fill",
                            side=side,
                            quantity=quantity,
                            price=str(fill_price),
                            fee=str(fee),
                            observed_at=stamp.isoformat(),
                            market_fill_verified=False,
                        )
                else:
                    event["action"] = "observe_only_scenario_complete"
            self._save(state)
            self.db.execute("INSERT INTO events VALUES (?,?,?)", (source, digest, canonical(event)))
        return True

    def snapshot(self):
        state = self._state()
        events = [
            json.loads(r[0]) for r in self.db.execute("SELECT body FROM events ORDER BY rowid")
        ]
        cash = Decimal(CONFIG["initial_cash"])
        position = 0
        fills = [e for e in events if e["action"] == "simulated_fill"]
        for event in fills:
            notional = Decimal(event["price"]) * event["quantity"]
            cash += (-notional if event["side"] == "buy" else notional) - Decimal(event["fee"])
            position += event["quantity"] * (1 if event["side"] == "buy" else -1)
        return dict(
            mode="paper_connectivity_test",
            real_execution_enabled=False,
            freshness_verified=False,
            market_fill_verified=False,
            state=state,
            source_records=len(events),
            fill_events=fills,
            accounting_matches=(
                cash == Decimal(state["cash"])
                and position == state["position"]
                and len(fills) == state["fills"]
            ),
            completed_roundtrip=state["phase"] == "complete",
            realized_test_pnl=str(cash - Decimal(CONFIG["initial_cash"]))
            if position == 0
            else None,
        )
