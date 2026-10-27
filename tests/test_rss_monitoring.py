import json
from datetime import UTC, datetime

import duckdb
import pytest

from trading_system.data_sources.rss_live import observe
from trading_system.data_sources.rss_snapshot import ITEMS
from trading_system.monitoring.rss import audit, local_file, main
from trading_system.storage.rss import RSSStore
from trading_system.storage.store import digest


@pytest.fixture
def saved(tmp_path):
    formulas = [f'=RssMarket("1234.T","{item}")' for item in ITEMS.values()]
    raw = dict(
        schema_version=1,
        status="read",
        formulas_before=formulas,
        formulas_after=formulas,
        values_before=[100, "15:30:00", "2026/01/05"],
        values_after=[100, "15:30:00", "2026/01/05"],
    )
    run, _ = observe(
        tmp_path / "unused.xlsx",
        "1234.T",
        tmp_path / "runs",
        samples=1,
        reader=lambda *_: raw,
        utcnow=lambda: datetime(2026, 1, 5, 8, tzinfo=UTC),
    )
    root = tmp_path / "store"
    with RSSStore(root) as store:
        store.import_run(run)
        store.export()
    return root


def snapshot(root):
    return {
        str(p.relative_to(root)): digest(p.read_bytes()) for p in root.rglob("*") if p.is_file()
    }


def parquet(root):
    manifest = json.loads((root / "parquet/current.json").read_bytes())
    return root / "parquet" / manifest["generation"] / "observations.parquet"


def test_consistent_read_does_not_modify_store(saved):
    before = snapshot(saved)
    result = audit(saved)
    assert result["status"] == "consistent"
    assert result["imports"] == result["observations"] == 1
    assert result["rss_connection_status"] == "unknown"
    assert result["freshness_verified"] is False
    assert snapshot(saved) == before


def test_missing_store_not_created(tmp_path):
    root = tmp_path / "absent"
    assert audit(root)["issues"] == ["catalog_missing"]
    assert not root.exists()


@pytest.mark.parametrize(
    "change,issue",
    [
        ("raw_missing", "raw_missing"),
        ("raw_corrupt", "raw_hash_mismatch"),
        ("manifest_missing", "manifest_missing"),
        ("manifest_corrupt", "invalid_manifest_or_parquet"),
        ("parquet_missing", "published_parquet_missing"),
        ("parquet_corrupt", "parquet_hash_mismatch"),
    ],
)
def test_missing_and_corrupt_files(saved, change, issue):
    if change.startswith("raw"):
        path = next((saved / "raw").glob("*.json"))
    elif change.startswith("manifest"):
        path = saved / "parquet/current.json"
    else:
        path = parquet(saved)
    if change.endswith("missing"):
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    before = snapshot(saved)
    assert issue in audit(saved)["issues"]
    assert snapshot(saved) == before


def test_catalog_values_checked_against_raw_and_parquet(saved):
    with duckdb.connect(str(saved / "rss.duckdb")) as db:
        db.execute("UPDATE observations SET price='999'")
    issues = audit(saved)["issues"]
    assert "raw_catalog_mismatch" in issues
    assert "parquet_catalog_mismatch" in issues


def test_checkpoint_gap_detected(saved):
    with duckdb.connect(str(saved / "rss.duckdb")) as db:
        db.execute("DELETE FROM observations")
    assert "checkpoint_observation_mismatch" in audit(saved)["issues"]


def test_other_complete_generations_not_called_failures(saved):
    with RSSStore(saved) as store:
        store.export()
    result = audit(saved)
    assert result["status"] == "consistent"
    assert result["other_generations"] == 1


def test_orphan_raw_and_incomplete_generation_reported(saved):
    (saved / "raw" / ("a" * 64 + ".json")).write_bytes(b"orphan")
    (saved / "parquet" / ("b" * 32)).mkdir()
    result = audit(saved)
    assert result["unreferenced_raw"] == result["incomplete_other_generations"] == 1
    assert set(result["issues"]) == {"unreferenced_raw", "incomplete_other_generations"}


def test_manifest_path_traversal_not_read(saved):
    path = saved / "parquet/current.json"
    manifest = json.loads(path.read_bytes())
    manifest["generation"] = "../../outside"
    path.write_text(json.dumps(manifest))
    assert "invalid_manifest_or_parquet" in audit(saved)["issues"]
    with pytest.raises(ValueError, match="unsafe_archive_path"):
        local_file(saved.resolve(), "../outside")


def test_raw_reference_path_not_followed(saved):
    with duckdb.connect(str(saved / "rss.duckdb")) as db:
        db.execute("UPDATE imports SET raw_path='../private.json'")
    assert "invalid_raw_reference" in audit(saved)["issues"]


def test_quality_issue_retained(saved, tmp_path):
    source = tmp_path / ("c" * 32)
    source.mkdir()
    (source / "000.json").write_bytes(b"invalid")
    with RSSStore(saved) as store:
        store.import_run(source)
        store.export()
    result = audit(saved)
    assert result["issues"] == ["observation_attention"]


def test_cli_failure_hides_exception_and_path(tmp_path, capsys):
    root = tmp_path / "secret"
    root.mkdir()
    (root / "rss.duckdb").write_bytes(b"bad database")
    assert main(["--root", str(root)]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "reason": "rss_audit_failed"}
