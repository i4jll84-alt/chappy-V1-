import json
from datetime import UTC, datetime

import pytest

from trading_system import rss_workflow as workflow
from trading_system.data_sources.rss_live import observe
from trading_system.data_sources.rss_snapshot import ITEMS


@pytest.fixture
def run(tmp_path):
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
    return run


def execute(tmp_path, **kwargs):
    return workflow.execute(
        store_root=tmp_path / "store", reports_root=tmp_path / "reports", **kwargs
    )


def test_saved_mode_never_observes_and_repeat_is_idempotent(tmp_path, run, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("saved mode must not access Excel")

    monkeypatch.setattr(workflow, "observe", forbidden)
    path, first = execute(tmp_path, run=run)
    assert first["status"] == "complete"
    assert first["completed_stages"] == ["validate_run", "import", "export", "audit"]
    assert json.loads(path.read_bytes()) == first
    _, second = execute(tmp_path, run=run)
    assert second["import"] == dict(imported=0, unchanged=1, attention=0)


def test_capture_routes_to_same_storage_and_audit(tmp_path, run, monkeypatch):
    def fake_capture(*args, **kwargs):
        assert kwargs["samples"] == 3
        return run, json.loads((run / "summary.json").read_bytes())

    monkeypatch.setattr(workflow, "observe", fake_capture)
    _, report = execute(tmp_path, workbook=tmp_path / "unused.xlsx", code="1234.T")
    assert report["status"] == "complete"
    assert report["completed_stages"][0] == "observe"
    assert report["freshness_verified"] is False


@pytest.mark.parametrize("name", ["summary.json", "000.json"])
def test_incomplete_run_stops_before_database_creation(tmp_path, run, name):
    (run / name).unlink()
    _, report = execute(tmp_path, run=run)
    assert report["status"] == "failed"
    assert report["stage"] == "validate_run"
    assert not (tmp_path / "store").exists()


def test_capture_exception_stops_without_import(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("secret workbook path")

    monkeypatch.setattr(workflow, "observe", fail)
    path, report = execute(tmp_path, workbook=tmp_path / "unused.xlsx", code="1234.T")
    assert report["stage"] == "observe"
    assert report["status"] == "failed"
    assert "secret" not in path.read_text()
    assert not (tmp_path / "store").exists()


def test_export_failure_preserves_import_and_saved_retry(tmp_path, run, monkeypatch):
    original = workflow.RSSStore.export

    def fail(self):
        raise OSError("export failed")

    monkeypatch.setattr(workflow.RSSStore, "export", fail)
    _, report = execute(tmp_path, run=run)
    assert report["stage"] == "export"
    assert report["completed_stages"] == ["validate_run", "import"]
    monkeypatch.setattr(workflow.RSSStore, "export", original)
    _, retry = execute(tmp_path, run=run)
    assert retry["status"] == "complete"
    assert retry["import"]["unchanged"] == 1


def test_other_unimported_run_keeps_overall_attention(tmp_path, run):
    other = run.parent / ("a" * 32)
    other.mkdir()
    (other / "000.json").write_bytes((run / "000.json").read_bytes())
    _, report = execute(tmp_path, run=run)
    assert report["status"] == "attention_required"
    assert report["coverage"]["unimported_samples"] == 1


def test_observation_error_is_archived_with_attention(tmp_path, run):
    path = run / "000.json"
    record = json.loads(path.read_bytes())
    record["raw"] = {"status": "error", "reason": "excel_not_registered"}
    record["status"] = "attention_required"
    record["excel_read_verified"] = False
    path.write_text(json.dumps(record))
    summary_path = run / "summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary.update(status="attention_required", excel_reads=0, attention_samples=1)
    summary_path.write_text(json.dumps(summary))
    _, report = execute(tmp_path, run=run)
    assert report["status"] == "attention_required"
    assert report["import"]["attention"] == 1


def test_invalid_modes_rejected_without_output(tmp_path):
    with pytest.raises(ValueError):
        execute(tmp_path)
    assert not (tmp_path / "reports").exists()
