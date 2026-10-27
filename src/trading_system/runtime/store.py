"""Exclusive owner plus SQLite atomic output/checkpoint/attempt publication.

The OS lock is reused from broker routing. A recovered RUNNING job belongs to
a dead/closed owner because no second writer can acquire that lock concurrently.
Only bounded, side-effect-free DATA handlers may use this retry queue.
"""

import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from trading_system.brokers.locking import single_writer
from trading_system.execution.store import digest
from trading_system.runtime.models import KINDS
from trading_system.storage.store import canonical


class JobStore:
    def __init__(self, root, now):
        self.lock = single_writer(root)
        self.lock.__enter__()
        self.owner = uuid4().hex
        try:
            self.db = sqlite3.connect(root / "lifecycle.sqlite", isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(
                    id TEXT PRIMARY KEY,job TEXT,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outputs(job TEXT PRIMARY KEY,payload TEXT,hash TEXT);
                CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,value TEXT NOT NULL);
            """)
            with self.transaction():
                for job in self.jobs():
                    if job["state"] == "running":
                        self.end_attempt(job, now, "interrupted")
                        job.update(
                            state="retry_wait",
                            reason="previous_owner_interrupted",
                            owner=None,
                            next_at=now.isoformat(),
                        )
                        self.save(job)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.__exit__(None, None, None)
            raise

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, canonical(value)))

    def jobs(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT value FROM jobs ORDER BY key")]

    def get(self, key):
        row = self.db.execute("SELECT value FROM jobs WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, job):
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?)", (job["key"], canonical(job)))

    def ensure(self, kind, day, scope, version, dependencies, inputs):
        if kind not in KINDS:
            raise ValueError("only_replayable_data_jobs_allowed")
        identity = dict(kind=kind, day=str(day), scope=scope, version=version)
        key = digest(identity)
        if self.get(key) is None:
            job = dict(
                **identity,
                key=key,
                dependencies=dependencies,
                inputs=inputs,
                state="pending",
                reason=None,
                attempts=0,
                attempt_id=None,
                owner=None,
                next_at=None,
                checkpoint={},
                output=None,
            )
            self.db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?)", (key, canonical(job)))
        return key

    def claim(self, job, now):
        with self.transaction():
            job.update(
                state="running",
                reason=None,
                owner=self.owner,
                attempt_id=uuid4().hex,
                attempts=job["attempts"] + 1,
            )
            job["inputs"]["dependencies"] = {
                key: self.output(key)["hash"] for key in job["dependencies"]
            }
            self.save(job)
            value = dict(
                started_at=now.isoformat(),
                ended_at=None,
                result="running",
                attempt=job["attempts"],
                owner=self.owner,
                inputs=job["inputs"],
            )
            self.db.execute(
                "INSERT INTO attempts VALUES(?,?,?)",
                (job["attempt_id"], job["key"], canonical(value)),
            )
        return job

    def checkpoint(self, job, value):
        current = self.get(job["key"])
        if (
            current["owner"] != self.owner
            or current["state"] != "running"
            or current["attempt_id"] != job["attempt_id"]
        ):
            raise ValueError("checkpoint_not_owned")
        job["checkpoint"] = value
        with self.transaction():
            self.save(job)

    def end_attempt(self, job, now, result):
        if job["attempt_id"] is None:
            return
        row = self.db.execute(
            "SELECT value FROM attempts WHERE id=?", (job["attempt_id"],)
        ).fetchone()
        value = json.loads(row[0])
        value.update(ended_at=now.isoformat(), result=result)
        self.db.execute(
            "UPDATE attempts SET value=? WHERE id=?", (canonical(value), job["attempt_id"])
        )

    def finish(self, job, now, state, reason=None, output=None, next_at=None):
        if state not in {"completed", "publication_wait", "retry_wait", "needs_review"}:
            raise ValueError("unsupported_job_result")
        with self.transaction():
            current = self.get(job["key"])
            if (
                current["owner"] != self.owner
                or current["state"] != "running"
                or current["attempt_id"] != job["attempt_id"]
            ):
                raise ValueError("completion_not_owned")
            if state == "completed":
                if output is None or type(output.get("count")) is not int or output["count"] < 0:
                    raise ValueError("missing_verified_output")
                payload = canonical(output)
                content_hash = digest(output)
                self.db.execute(
                    "INSERT INTO outputs VALUES(?,?,?)", (job["key"], payload, content_hash)
                )
                job["output"] = dict(
                    location="sqlite:outputs/" + job["key"],
                    hash=content_hash,
                    verified=True,
                    count=output["count"],
                )
            self.end_attempt(job, now, reason or state)
            job.update(
                state=state,
                reason=reason,
                owner=None,
                next_at=next_at.isoformat() if next_at else None,
            )
            self.save(job)

    def output(self, key):
        row = self.db.execute("SELECT payload,hash FROM outputs WHERE job=?", (key,)).fetchone()
        if not row:
            raise ValueError("output_missing")
        payload = json.loads(row[0])
        if digest(payload) != row[1]:
            raise ValueError("output_hash_mismatch")
        return dict(payload=payload, hash=row[1])

    def record_status(self, value, normal=False):
        if normal:
            value = {**value, "last_normal_exit": value["at"]}
        with self.transaction():
            self.set_meta("status", value)
            self.db.execute("INSERT INTO history(value) VALUES(?)", (canonical(value),))
            if normal:
                self.set_meta("last_normal_exit", value["at"])

    def close(self):
        self.db.close()
        self.lock.__exit__(None, None, None)
