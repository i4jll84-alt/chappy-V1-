"""Broker-neutral immutable observations, transactional checkpoints, verified Parquet."""

import base64
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import pyarrow.parquet as pq

from trading_system.point_in_time.policy import aware_utc
from trading_system.storage.store import atomic_write, canonical, digest, normalized_payload


class MarketDataStore:
    """Caller holds the common single-writer lock for this root for its full lifetime."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        (self.root / "raw").mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self.root / "market-data.duckdb"))
        self.db.execute("SET TimeZone='UTC'")
        self.db.execute("""
            CREATE SEQUENCE IF NOT EXISTS market_observation_seq;
            CREATE TABLE IF NOT EXISTS observations (
                run_id VARCHAR, sample INTEGER, broker VARCHAR, code VARCHAR,
                operation VARCHAR, observed_at TIMESTAMPTZ, available_at TIMESTAMPTZ,
                payload_hash VARCHAR, previous_payload_hash VARCHAR, relation VARCHAR,
                raw_path VARCHAR, raw_sha256 VARCHAR, record_json VARCHAR,
                ingest_order BIGINT UNIQUE,
                PRIMARY KEY(run_id,sample));
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id VARCHAR PRIMARY KEY, sample INTEGER, raw_sha256 VARCHAR);
        """)

    def close(self):
        self.db.close()

    def _path(self, run_id, sample):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id) or type(sample) is not int or sample < 1:
            raise ValueError("invalid_observation_identity")
        return self.root / "raw" / run_id / f"{sample:06d}.json"

    def save(self, run_id, sample, record, raw):
        envelope = {
            "schema_version": 1,
            "run_id": run_id,
            "sample": sample,
            "record": record,
            "body_base64": base64.b64encode(raw).decode("ascii") if raw is not None else None,
            "body_sha256": digest(raw) if raw is not None else None,
        }
        path = self._path(run_id, sample)
        content = canonical(envelope).encode()
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError("immutable_observation_conflict")
        else:
            atomic_write(path, content)
        self._import(path)

    def _inspect(self, path):
        content = path.read_bytes()
        envelope = json.loads(content)
        if envelope["schema_version"] != 1 or path != self._path(
            envelope["run_id"], envelope["sample"]
        ):
            raise ValueError("invalid_raw_identity")
        record = envelope["record"]
        if record["real_execution_enabled"] is not False or record["order_requests"] != 0:
            raise ValueError("invalid_execution_evidence")
        started = aware_utc(datetime.fromisoformat(record["read_started_at"]))
        completed = aware_utc(datetime.fromisoformat(record["read_completed_at"]))
        # Retain clock regressions as attention records, never as usable market data.
        if started > completed and "observation_clock_regressed" not in record["issues"]:
            raise ValueError("unmarked_clock_regression")
        payload_hash = None
        encoded = envelope["body_base64"]
        if encoded is not None:
            if record["operation"] != "board":
                raise ValueError("authentication_response_must_not_be_saved")
            body = base64.b64decode(encoded, validate=True)
            if digest(body) != envelope["body_sha256"]:
                raise ValueError("raw_body_hash_mismatch")
            try:
                payload_hash = digest(canonical(normalized_payload(json.loads(body))).encode())
            except (ValueError, UnicodeError):
                payload_hash = digest(body)
        elif envelope["body_sha256"] is not None:
            raise ValueError("raw_body_missing")
        return content, envelope, completed, payload_hash

    def _import(self, path):
        content, envelope, completed, payload_hash = self._inspect(path)
        run_id, sample, record = envelope["run_id"], envelope["sample"], envelope["record"]
        sha = digest(content)
        old = self.db.execute(
            "SELECT raw_sha256,record_json FROM observations WHERE run_id=? AND sample=?",
            [run_id, sample],
        ).fetchone()
        if old:
            if old != (sha, canonical(record)):
                raise ValueError("raw_catalog_mismatch")
            return
        checkpoint = self.db.execute(
            "SELECT sample FROM checkpoints WHERE run_id=?", [run_id]
        ).fetchone()
        if sample != (checkpoint[0] + 1 if checkpoint else 1):
            raise ValueError("observation_sequence_gap")
        previous = self.db.execute(
            "SELECT payload_hash FROM observations WHERE broker=? AND code=? "
            "AND payload_hash IS NOT NULL "
            "ORDER BY ingest_order DESC LIMIT 1",
            [record["broker"], record["code"]],
        ).fetchone()
        prev_hash = previous[0] if previous else None
        relation = (
            "no_payload"
            if payload_hash is None
            else "first_snapshot"
            if prev_hash is None
            else "identical"
            if payload_hash == prev_hash
            else "changed_snapshot_revision_unverified"
        )
        self.db.execute("BEGIN")
        try:
            self.db.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "nextval('market_observation_seq'))",
                [
                    run_id,
                    sample,
                    record["broker"],
                    record["code"],
                    record["operation"],
                    completed,
                    completed,
                    payload_hash,
                    prev_hash,
                    relation,
                    path.relative_to(self.root).as_posix(),
                    sha,
                    canonical(record),
                ],
            )
            self.db.execute(
                "INSERT INTO checkpoints VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE "
                "SET sample=excluded.sample,raw_sha256=excluded.raw_sha256",
                [run_id, sample, sha],
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def recover(self):
        # Run order does not change immutable timestamps. Each run is imported sequentially.
        runs = sorted((self.root / "raw").iterdir(), key=lambda p: p.stat().st_mtime_ns)
        for run in runs:
            if run.is_dir():
                for path in sorted(run.glob("*.json")):
                    self._import(path)

    def audit(self):
        paths = sorted((self.root / "raw").glob("*/*.json"))
        catalog = self.db.execute(
            "SELECT run_id,sample,raw_path,raw_sha256,record_json,payload_hash,observed_at,"
            "available_at,broker,code,operation,previous_payload_hash,relation "
            "FROM observations ORDER BY ingest_order"
        ).fetchall()
        if len(paths) != len(catalog):
            raise ValueError("raw_catalog_count_mismatch")
        last = {}
        previous_payloads = {}
        for (
            run_id,
            sample,
            relative,
            sha,
            record_json,
            payload_hash,
            observed,
            available,
            broker,
            code,
            operation,
            prev_hash,
            relation,
        ) in catalog:
            path = self._path(run_id, sample)
            if path.relative_to(self.root).as_posix() != relative:
                raise ValueError("raw_path_mismatch")
            content, envelope, completed, raw_payload_hash = self._inspect(path)
            record = envelope["record"]
            key = (record["broker"], record["code"])
            expected_prev = previous_payloads.get(key)
            expected_relation = (
                "no_payload"
                if raw_payload_hash is None
                else "first_snapshot"
                if expected_prev is None
                else "identical"
                if raw_payload_hash == expected_prev
                else "changed_snapshot_revision_unverified"
            )
            if (
                digest(content) != sha
                or canonical(envelope["record"]) != record_json
                or payload_hash != raw_payload_hash
                or observed != completed
                or available != completed
                or (broker, code, operation) != (*key, record["operation"])
                or prev_hash != expected_prev
                or relation != expected_relation
            ):
                raise ValueError("raw_catalog_content_mismatch")
            if raw_payload_hash is not None:
                previous_payloads[key] = raw_payload_hash
            last[run_id] = (sample, sha)
        checkpoints = self.db.execute("SELECT run_id,sample,raw_sha256 FROM checkpoints").fetchall()
        if {run: (seq, sha) for run, seq, sha in checkpoints} != last:
            raise ValueError("checkpoint_mismatch")
        return {"observations": len(catalog), "runs": len(last), "raw_catalog_matches": True}

    def export(self):
        audit = self.audit()
        generation = uuid4().hex
        folder = self.root / "parquet" / generation
        folder.mkdir(parents=True)
        tables = {}
        for name in ("observations", "checkpoints"):
            table = self.db.execute(f"SELECT * FROM {name} ORDER BY run_id,sample").to_arrow_table()
            path = folder / f"{name}.parquet"
            pq.write_table(table, path, compression="zstd")
            # Compare every cell and schema; row counts alone cannot establish consistency.
            if not pq.read_table(path).equals(table):
                raise ValueError("parquet_content_mismatch")
            tables[name] = {"rows": table.num_rows, "sha256": digest(path.read_bytes())}
        manifest = {"generation": generation, "tables": tables, **audit, "parquet_matches": True}
        atomic_write(folder / "manifest.json", canonical(manifest).encode())
        atomic_write(self.root / "parquet" / "current.json", canonical(manifest).encode())
        return manifest
