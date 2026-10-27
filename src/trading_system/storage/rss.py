"""Offline import of immutable RSS observation files; never connects to Excel."""

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import pyarrow.parquet as pq

from trading_system.data_sources.rss_live import validate_read
from trading_system.point_in_time.policy import available_at, aware_utc
from trading_system.storage.store import atomic_write, canonical, digest


def inspect_record(body: bytes, imported_at: datetime) -> dict:
    """Revalidate Raw rather than trusting saved normalized values or freshness flags."""
    try:
        record = json.loads(body)
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != 1
            or record.get("source") != "rakuten_ms2_rss_excel_cells"
        ):
            raise ValueError
        started = aware_utc(datetime.fromisoformat(record["read_started_at"]))
        completed = aware_utc(datetime.fromisoformat(record["read_completed_at"]))
        observed = aware_utc(datetime.fromisoformat(record["observed_at"]))
        if not started <= completed <= imported_at or completed != observed:
            raise ValueError
        raw = record["raw"]
        if not isinstance(raw, dict) or not isinstance(record["code"], str):
            raise ValueError
        checked = validate_read(raw, record["code"], completed)
        # Preserve an observer's warning without claiming to reproduce its clock window.
        if record.get("status") != "observed" and not checked["issues"]:
            checked["issues"].append("observer_attention")
            checked["status"] = "attention_required"
        return {**checked, "available_at": available_at(completed)}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {"status": "rejected", "issues": ["invalid_observation_record"]}


class RSSStore:
    """Single writer; record and import checkpoint commit in one transaction."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self.root / "rss.duckdb"))
        self.db.execute("SET TimeZone='UTC'")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS imports (
                run_id VARCHAR, sample INTEGER, raw_sha256 VARCHAR, raw_path VARCHAR,
                imported_at TIMESTAMPTZ, PRIMARY KEY(run_id,sample));
            CREATE TABLE IF NOT EXISTS observations (
                run_id VARCHAR, sample INTEGER, code VARCHAR, observed_at TIMESTAMPTZ,
                available_at TIMESTAMPTZ, price VARCHAR, quote_time VARCHAR,
                current_date VARCHAR, quote_date DATE, published_at TIMESTAMPTZ,
                source_updated_at TIMESTAMPTZ, rss_connection_status VARCHAR,
                freshness_verified BOOLEAN, status VARCHAR, issues_json VARCHAR,
                PRIMARY KEY(run_id,sample));
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def import_run(self, run: Path) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", run.name) or not run.is_dir():
            raise ValueError("invalid_run_directory")
        paths = sorted(p for p in run.iterdir() if re.fullmatch(r"\d{3}\.json", p.name))
        if not paths or len(paths) > 60:
            raise ValueError("invalid_sample_count")
        counts = {"imported": 0, "unchanged": 0, "attention": 0}
        for path in paths:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("observation_too_large")
            body = path.read_bytes()
            if len(body) > 1024 * 1024:
                raise ValueError("observation_too_large")
            sha = digest(body)
            relative = Path("raw") / f"{sha}.json"
            archived = self.root / relative
            if archived.exists():
                if digest(archived.read_bytes()) != sha:
                    raise ValueError("raw_archive_mismatch")
            else:
                atomic_write(archived, body)
            sample = int(path.stem)
            previous = self.db.execute(
                "SELECT raw_sha256 FROM imports WHERE run_id=? AND sample=?",
                [run.name, sample],
            ).fetchone()
            if previous:
                if previous[0] != sha:
                    # Both byte versions remain in Raw. Never silently replace history.
                    raise ValueError("observation_identity_conflict")
                counts["unchanged"] += 1
                status = self.db.execute(
                    "SELECT status FROM observations WHERE run_id=? AND sample=?",
                    [run.name, sample],
                ).fetchone()
                if status is None:
                    raise ValueError("checkpoint_without_observation")
                counts["attention"] += int(status[0] != "observed")
                continue
            now = datetime.now(UTC)
            record = inspect_record(body, now)
            values = record.get("normalized", {})
            self.db.execute("BEGIN TRANSACTION")
            try:
                self.db.execute(
                    "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,"
                    "'unknown',false,?,?)",
                    [
                        run.name,
                        sample,
                        record.get("code"),
                        record.get("observed_at"),
                        record.get("available_at"),
                        values.get("price"),
                        values.get("quote_time"),
                        values.get("current_date"),
                        record["status"],
                        canonical(record["issues"]),
                    ],
                )
                self.db.execute(
                    "INSERT INTO imports VALUES (?,?,?,?,?)",
                    [run.name, sample, sha, relative.as_posix(), now],
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            counts["imported"] += 1
            counts["attention"] += int(record["status"] != "observed")
        return counts

    def export(self) -> dict:
        """Publish a generation manifest only after both Parquet tables are complete."""
        generation = uuid4().hex
        folder = self.root / "parquet" / generation
        folder.mkdir(parents=True)
        tables = {}
        for name in ("imports", "observations"):
            table = self.db.execute(f"SELECT * FROM {name} ORDER BY run_id,sample").to_arrow_table()
            path = folder / f"{name}.parquet"
            pq.write_table(table, path)
            if pq.read_metadata(path).num_rows != table.num_rows:
                raise ValueError("export_count_mismatch")
            tables[name] = {"rows": table.num_rows, "sha256": digest(path.read_bytes())}
        manifest = {"schema_version": 1, "generation": generation, "tables": tables}
        atomic_write(self.root / "parquet" / "current.json", canonical(manifest).encode())
        return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/rss-store"))
    parser.add_argument("--run", type=Path)
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args(argv)
    if (args.run is None) == (not args.export_only):
        parser.error("specify exactly one of --run or --export-only")
    try:
        with RSSStore(args.root) as store:
            counts = store.import_run(args.run) if args.run else {}
            manifest = store.export()
        print(json.dumps({"status": "complete", **counts, **manifest}))
        return 3 if counts.get("attention") else 0
    except (OSError, ValueError, duckdb.Error):
        print(json.dumps({"status": "failed", "reason": "rss_storage_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
