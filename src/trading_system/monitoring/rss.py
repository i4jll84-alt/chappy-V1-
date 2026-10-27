"""Read-only audit of saved RSS archives, catalog and published Parquet."""

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from trading_system.storage.rss import inspect_record
from trading_system.storage.store import canonical, digest


def local_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError("unsafe_archive_path")
    return path


def audit(root: Path) -> dict:
    root = root.resolve()
    issues = set()
    result = {
        "schema_version": 1,
        "status": "attention_required",
        "issues": [],
        "imports": 0,
        "observations": 0,
        "unreferenced_raw": 0,
        "other_generations": 0,
        "incomplete_other_generations": 0,
        "rss_connection_status": "unknown",
        "freshness_verified": False,
    }
    database = local_file(root, "rss.duckdb")
    if not database.is_file():
        return {**result, "issues": ["catalog_missing"]}
    with duckdb.connect(str(database), read_only=True) as db:
        db.execute("SET TimeZone='UTC'")
        db.execute("BEGIN TRANSACTION")
        kinds = dict(
            db.execute(
                "SELECT table_name,table_type FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name IN ('imports','observations')"
            ).fetchall()
        )
        if kinds != {"imports": "BASE TABLE", "observations": "BASE TABLE"}:
            raise ValueError("invalid_catalog_schema")
        tables = ("imports", "observations")
        schemas = {}
        for name in tables:
            result[name] = db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            schemas[name] = db.execute(f"SELECT * FROM {name} LIMIT 0").to_arrow_table().schema
        if not result["imports"]:
            issues.add("no_imports")
        if db.execute(
            "SELECT count(*) FROM imports i FULL OUTER JOIN observations o "
            "USING(run_id,sample) WHERE i.run_id IS NULL OR o.run_id IS NULL"
        ).fetchone()[0]:
            issues.add("checkpoint_observation_mismatch")
        # Stream all catalog rows; ordinary day-long captures already exceed 10,000.
        cursor = db.execute(
            "SELECT i.*,o.* FROM imports i LEFT JOIN observations o USING(run_id,sample) "
            "ORDER BY i.run_id,i.sample"
        )

        def pairs():
            while rows := cursor.fetchmany(1024):
                for row in rows:
                    yield (
                        dict(zip(schemas["imports"].names, row[:5], strict=True)),
                        dict(zip(schemas["observations"].names, row[5:], strict=True)),
                    )

        referenced = set()
        for item, observed in pairs():
            sha = item["raw_sha256"]
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                issues.add("invalid_raw_reference")
                continue
            relative = f"raw/{sha}.json"
            if item["raw_path"] != relative:
                issues.add("invalid_raw_reference")
                continue
            referenced.add(f"{sha}.json")
            path = local_file(root, relative)
            if not path.is_file():
                issues.add("raw_missing")
                continue
            if path.stat().st_size > 1024 * 1024:
                issues.add("raw_too_large")
                continue
            body = path.read_bytes()
            if digest(body) != sha:
                issues.add("raw_hash_mismatch")
                continue
            checked = inspect_record(body, item["imported_at"])
            values = checked.get("normalized", {})
            expected = {
                "run_id": item["run_id"],
                "sample": item["sample"],
                "code": checked.get("code"),
                "observed_at": datetime.fromisoformat(checked["observed_at"])
                if checked.get("observed_at")
                else None,
                "available_at": checked.get("available_at"),
                "price": values.get("price"),
                "quote_time": values.get("quote_time"),
                "current_date": values.get("current_date"),
                "quote_date": None,
                "published_at": None,
                "source_updated_at": None,
                "rss_connection_status": "unknown",
                "freshness_verified": False,
                "status": checked["status"],
                "issues_json": canonical(checked["issues"]),
            }
            if observed != expected:
                issues.add("raw_catalog_mismatch")
            if checked["status"] != "observed":
                issues.add("observation_attention")
        raw_dir = local_file(root, "raw")
        if raw_dir.is_dir():
            result["unreferenced_raw"] = len({p.name for p in raw_dir.glob("*.json")} - referenced)
            if result["unreferenced_raw"]:
                issues.add("unreferenced_raw")
        manifest_path = local_file(root, "parquet/current.json")
        generation = None
        if not manifest_path.is_file():
            issues.add("manifest_missing")
        else:
            before = manifest_path.read_bytes()
            try:
                manifest = json.loads(before)
                generation = manifest["generation"]
                if manifest["schema_version"] != 1 or not re.fullmatch(r"[0-9a-f]{32}", generation):
                    raise ValueError
                for name in tables:
                    meta = manifest["tables"][name]
                    path = local_file(root, f"parquet/{generation}/{name}.parquet")
                    if not path.is_file():
                        issues.add("published_parquet_missing")
                        continue
                    with path.open("rb") as stream:
                        actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                    if actual_hash != meta["sha256"]:
                        issues.add("parquet_hash_mismatch")
                        continue
                    if pq.read_metadata(path).num_rows != meta["rows"]:
                        issues.add("parquet_count_mismatch")
                    if not pq.read_schema(path).equals(schemas[name]):
                        issues.add("parquet_catalog_mismatch")
                        continue
                    difference = db.execute(
                        f"SELECT count(*) FROM ((SELECT * FROM {name} EXCEPT ALL "
                        "SELECT * FROM read_parquet(?)) UNION ALL "
                        f"(SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM {name}))",
                        [str(path), str(path)],
                    ).fetchone()[0]
                    if difference:
                        issues.add("parquet_catalog_mismatch")
                if manifest_path.read_bytes() != before:
                    issues.add("manifest_changed_during_audit")
            except (ValueError, KeyError, TypeError):
                issues.add("invalid_manifest_or_parquet")
        parquet_dir = local_file(root, "parquet")
        if parquet_dir.is_dir():
            for folder in parquet_dir.iterdir():
                if folder.is_dir() and re.fullmatch(r"[0-9a-f]{32}", folder.name):
                    local_file(root, f"parquet/{folder.name}")
                    if folder.name == generation:
                        continue
                    result["other_generations"] += 1
                    if not all((folder / f"{name}.parquet").is_file() for name in tables):
                        result["incomplete_other_generations"] += 1
            if result["incomplete_other_generations"]:
                issues.add("incomplete_other_generations")
    return {
        **result,
        "status": "attention_required" if issues else "consistent",
        "issues": sorted(issues),
    }


def audit_runs(root: Path, source: Path) -> dict:
    """Reconcile all saved run directories against import checkpoints, without recovery."""
    root, source = root.resolve(), source.resolve()
    issues = set()
    result = {
        "status": "attention_required",
        "issues": [],
        "runs": 0,
        "source_samples": 0,
        "matched_samples": 0,
        "unimported_samples": 0,
        "changed_samples": 0,
        "imports_without_source": 0,
        "complete_runs": 0,
    }
    if not source.is_dir():
        return {**result, "issues": ["source_directory_missing"]}
    database = local_file(root, "rss.duckdb")
    imported = {}
    if database.is_file():
        with duckdb.connect(str(database), read_only=True) as db:
            cursor = db.execute("SELECT run_id,sample,raw_sha256 FROM imports")
            while rows := cursor.fetchmany(1024):
                imported.update({(run, sample): sha for run, sample, sha in rows})
    else:
        issues.add("catalog_missing")
    folders = sorted(
        p for p in source.iterdir() if p.is_dir() and re.fullmatch(r"[0-9a-f]{32}", p.name)
    )
    if not folders:
        issues.add("no_source_runs")
    seen = set()
    for folder in folders:
        local_file(source, folder.name)
        result["runs"] += 1
        paths = sorted(p for p in folder.iterdir() if re.fullmatch(r"\d{3}\.json", p.name))
        if len(paths) > 60:
            raise ValueError("audit_sample_limit")
        records = []
        for path in paths:
            local_file(source, f"{folder.name}/{path.name}")
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("audit_file_limit")
            body = path.read_bytes()
            key = (folder.name, int(path.stem))
            seen.add(key)
            result["source_samples"] += 1
            if key not in imported:
                result["unimported_samples"] += 1
                issues.add("source_samples_not_imported")
            elif digest(body) != imported[key]:
                result["changed_samples"] += 1
                issues.add("source_import_hash_mismatch")
            else:
                result["matched_samples"] += 1
            try:
                record = json.loads(body)
                if (
                    not isinstance(record, dict)
                    or record.get("schema_version") != 1
                    or record.get("source") != "rakuten_ms2_rss_excel_cells"
                    or record.get("status") not in ("observed", "attention_required")
                    or type(record.get("excel_read_verified")) is not bool
                ):
                    raise ValueError
                records.append(record)
            except (ValueError, TypeError):
                issues.add("invalid_source_record")
        summary_path = local_file(source, f"{folder.name}/summary.json")
        if not summary_path.is_file():
            issues.add("run_summary_missing")
            continue
        if summary_path.stat().st_size > 1024 * 1024:
            raise ValueError("audit_file_limit")
        try:
            summary = json.loads(summary_path.read_bytes())
            count = summary["samples"]
            if summary.get("schema_version") != 1 or type(count) is not int or not 1 <= count <= 60:
                raise ValueError
            if [p.name for p in paths] != [f"{i:03d}.json" for i in range(count)]:
                issues.add("run_sample_sequence_mismatch")
                continue
            if len(records) != count:
                continue
            attention = sum(r["status"] != "observed" for r in records)
            reads = sum(r["excel_read_verified"] for r in records)
            if (
                type(summary.get("attention_samples")) is not int
                or type(summary.get("excel_reads")) is not int
                or summary["attention_samples"] != attention
                or summary["excel_reads"] != reads
                or summary.get("status") != ("attention_required" if attention else "observed")
            ):
                issues.add("run_summary_counts_mismatch")
                continue
            result["complete_runs"] += 1
            if attention:
                issues.add("source_observation_attention")
        except (ValueError, TypeError, KeyError, AttributeError):
            issues.add("invalid_run_summary")
    result["imports_without_source"] = len(set(imported) - seen)
    if result["imports_without_source"]:
        issues.add("imports_without_source")
    return {
        **result,
        "status": "attention_required" if issues else "consistent",
        "issues": sorted(issues),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/rss-store"))
    parser.add_argument("--runs", type=Path, help="Compare every run in this source directory")
    args = parser.parse_args(argv)
    try:
        result = audit(args.root)
        if args.runs is not None:
            result["coverage"] = audit_runs(args.root, args.runs)
            if result["coverage"]["status"] != "consistent":
                result["status"] = "attention_required"
    except (OSError, ValueError, TypeError, duckdb.Error):
        print(json.dumps({"status": "failed", "reason": "rss_audit_failed"}))
        return 2
    print(json.dumps(result))
    return 0 if result["status"] == "consistent" else 3


if __name__ == "__main__":
    raise SystemExit(main())
