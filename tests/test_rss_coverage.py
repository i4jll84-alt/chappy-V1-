import json
from datetime import UTC, datetime

import pytest

from trading_system.data_sources.rss_live import observe
from trading_system.data_sources.rss_snapshot import ITEMS
from trading_system.monitoring.rss import audit_runs, main
from trading_system.storage.rss import RSSStore
from trading_system.storage.store import digest


@pytest.fixture
def setup(tmp_path):
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
        samples=2,
        reader=lambda *_: raw,
        sleeper=lambda _: None,
        utcnow=lambda: datetime(2026, 1, 5, 8, tzinfo=UTC),
    )
    root = tmp_path / "store"
    with RSSStore(root) as store:
        store.import_run(run)
        store.export()
    return root, run


def test_complete_coverage_readonly(setup):
    root, run = setup
    before = {str(p): digest(p.read_bytes()) for p in root.parent.rglob("*") if p.is_file()}
    result = audit_runs(root, run.parent)
    assert result["status"] == "consistent"
    assert result["complete_runs"] == 1
    assert result["matched_samples"] == 2
    assert {str(p): digest(p.read_bytes()) for p in root.parent.rglob("*") if p.is_file()} == before


def test_not_imported_run_detected(setup):
    root, run = setup
    other = run.parent / ("a" * 32)
    other.mkdir()
    for path in run.iterdir():
        (other / path.name).write_bytes(path.read_bytes())
    result = audit_runs(root, run.parent)
    assert result["unimported_samples"] == 2
    assert "source_samples_not_imported" in result["issues"]


@pytest.mark.parametrize(
    "change,issue",
    [
        ("summary_missing", "run_summary_missing"),
        ("sample_missing", "run_sample_sequence_mismatch"),
        ("sample_changed", "source_import_hash_mismatch"),
        ("summary_count", "run_summary_counts_mismatch"),
        ("bad_summary", "invalid_run_summary"),
        ("bad_sample", "invalid_source_record"),
    ],
)
def test_incomplete_or_changed_runs(setup, change, issue):
    root, run = setup
    if change == "summary_missing":
        (run / "summary.json").unlink()
    elif change == "sample_missing":
        (run / "001.json").unlink()
    elif change == "sample_changed":
        path = run / "001.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif change == "summary_count":
        path = run / "summary.json"
        summary = json.loads(path.read_bytes())
        summary["excel_reads"] = 0
        path.write_text(json.dumps(summary))
    elif change == "bad_summary":
        (run / "summary.json").write_bytes(b"null")
    else:
        (run / "001.json").write_bytes(b"null")
    assert issue in audit_runs(root, run.parent)["issues"]


def test_source_directory_missing_no_creation(tmp_path):
    source = tmp_path / "missing"
    assert audit_runs(tmp_path / "store", source)["issues"] == ["source_directory_missing"]
    assert not source.exists()


def test_import_without_source_and_cli_status(setup, capsys):
    root, run = setup
    (run / "001.json").unlink()
    assert audit_runs(root, run.parent)["imports_without_source"] == 1
    assert main(["--root", str(root), "--runs", str(run.parent)]) == 3
    assert json.loads(capsys.readouterr().out)["coverage"]["imports_without_source"] == 1


def test_missing_catalog_and_empty_sources(tmp_path):
    source = tmp_path / "runs"
    source.mkdir()
    result = audit_runs(tmp_path / "store", source)
    assert result["issues"] == ["catalog_missing", "no_source_runs"]
