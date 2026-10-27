"""Deadline/recovery acceptance tests: fixed data and virtual clocks, no Excel/API."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_rss_continuous import CODE, NOW, body, raw

from trading_system import rss_continuous as service
from trading_system.data_sources.rss_live import observe
from trading_system.monitoring.rss import audit, audit_runs
from trading_system.storage.rss import RSSStore


def execute(root, clock, **kwargs):
    return service.run(
        workbook=root / "missing.xlsx",
        root=root,
        code=CODE,
        paper=True,
        utcnow=lambda: clock[0],
        sleeper=lambda _: None,
        **kwargs,
    )


def forbidden(*args, **kwargs):
    pytest.fail("External read attempted after cutoff or in recovery mode")


def test_current_read_finishes_at_deadline_without_next_read(tmp_path):
    clock = [datetime(2026, 1, 5, 6, 59, 59, tzinfo=UTC)]
    calls = []

    def observer(workbook, code, runs, **kwargs):
        def reader(*_):
            calls.append(clock[0])
            clock[0] += timedelta(seconds=4)
            return raw(clock[0])

        return observe(
            workbook,
            code,
            runs,
            **kwargs,
            reader=reader,
            utcnow=lambda: clock[0],
            sleeper=lambda _: None,
        )

    result = execute(tmp_path, clock, observer=observer)
    assert len(calls) == result["samples"] == 1
    assert result["stop_reason"] == "daytime_1600"
    assert result["normal_exit"] and result["status"] == "stopped"
    assert result["stop_delay_seconds"] == 3
    assert result["export"]["tables"]["observations"]["rows"] == 1
    assert result["coverage"]["unimported_samples"] == 0
    summary = json.loads(next((tmp_path / "runs").glob("*/summary.json")).read_bytes())
    assert summary["samples"] == 1 and summary["capture_stopped_early"]
    assert not result["real_execution_enabled"]
    assert result["paper"]["state"]["fills"] == 0


def test_deadline_during_interval_starts_no_second_read(tmp_path):
    clock = [datetime(2026, 1, 5, 6, 59, 58, tzinfo=UTC)]
    calls = []

    def observer(workbook, code, runs, **kwargs):
        def reader(*_):
            calls.append(clock[0])
            return raw(clock[0])

        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

        return observe(
            workbook,
            code,
            runs,
            **kwargs,
            reader=reader,
            utcnow=lambda: clock[0],
            sleeper=sleep,
        )

    result = execute(tmp_path, clock, observer=observer)
    assert len(calls) == result["samples"] == 1
    assert result["stop_reason"] == "daytime_1600"
    assert result["stop_delay_seconds"] == 0


def test_boundary_between_loop_and_sampler_creates_no_empty_run(tmp_path):
    clock = [datetime(2026, 1, 5, 6, 59, 59, tzinfo=UTC)]

    def observer(workbook, code, runs, **kwargs):
        clock[0] += timedelta(seconds=1)
        return observe(workbook, code, runs, **kwargs, reader=forbidden)

    result = execute(tmp_path, clock, observer=observer)
    assert result["samples"] == 0 and result["normal_exit"]
    assert list((tmp_path / "runs").iterdir()) == []


@pytest.mark.parametrize("hour,minute", [(7, 10), (10, 0), (13, 0)])
def test_evening_start_recovers_with_no_excel_reads(tmp_path, hour, minute):
    folder = tmp_path / "runs" / ("a" * 32)
    folder.mkdir(parents=True)
    original = body()
    (folder / "000.json").write_bytes(original)
    (tmp_path / "STOP").write_text("stop requested")
    clock = [datetime(2026, 1, 5, hour, minute, tzinfo=UTC)]
    result = execute(tmp_path, clock, observer=forbidden)
    assert result["purpose"] == "postprocessing"
    assert result["normal_exit"] and result["stop_reason"] == "postprocessing_complete"
    assert result["samples"] == 0 and result["recovered_samples"] == 1
    assert result["coverage"]["unimported_samples"] == 0
    assert "run_summary_missing" in result["coverage"]["issues"]
    assert not (folder / "summary.json").exists()
    assert (folder / "000.json").read_bytes() == original
    assert result["paper"]["state"]["fills"] == 0
    first_session = (tmp_path / "sessions" / (result["session_id"] + ".json")).read_bytes()
    again = execute(tmp_path, clock, observer=forbidden)
    assert again["recovered_samples"] == 0
    assert again["audit"]["imports"] == again["paper"]["source_records"] == 1
    assert again["paper"]["state"] == result["paper"]["state"]
    assert (tmp_path / "sessions" / (result["session_id"] + ".json")).read_bytes() == first_session


def test_recovery_crossing_cutoff_starts_no_capture(tmp_path, monkeypatch):
    folder = tmp_path / "runs" / ("b" * 32)
    folder.mkdir(parents=True)
    (folder / "000.json").write_bytes(body())
    clock = [datetime(2026, 1, 5, 6, 59, 59, tzinfo=UTC)]
    original = RSSStore.import_run

    def slow_import(self, folder):
        clock[0] += timedelta(seconds=2)
        return original(self, folder)

    monkeypatch.setattr(RSSStore, "import_run", slow_import)
    result = execute(tmp_path, clock, observer=forbidden)
    assert result["recovered_samples"] == 1 and result["samples"] == 0
    assert result["stop_reason"] == "daytime_1600" and result["normal_exit"]


def test_resume_after_midnight_keeps_original_deadline(tmp_path):
    clock = [datetime(2026, 1, 5, 1, tzinfo=UTC)]

    def observer(workbook, code, runs, **kwargs):
        result = observe(
            workbook,
            code,
            runs,
            samples=1,
            reader=lambda *_: raw(clock[0]),
            utcnow=lambda: clock[0],
        )
        clock[0] += timedelta(days=1)
        return result

    result = execute(tmp_path, clock, observer=observer)
    assert result["samples"] == 1 and result["stop_reason"] == "daytime_1600"
    assert result["scheduled_stop_at"] == "2026-01-05T16:00:00+09:00"


def test_export_failure_is_not_normal_and_can_be_recovered(tmp_path, monkeypatch):
    folder = tmp_path / "runs" / ("c" * 32)
    folder.mkdir(parents=True)
    (folder / "000.json").write_bytes(body())
    clock = [NOW]

    def fail(self):
        raise OSError("injected save failure")

    with monkeypatch.context() as m:
        m.setattr(RSSStore, "export", fail)
        with pytest.raises(OSError):
            execute(tmp_path, clock, recover_only=True, observer=forbidden)
    status = json.loads((tmp_path / "status.json").read_bytes())
    assert status["status"] == "failed" and not status["normal_exit"]
    restored = execute(tmp_path, clock, recover_only=True, observer=forbidden)
    assert restored["normal_exit"] and restored["recovered_samples"] == 0
    assert restored["audit"]["imports"] == restored["paper"]["source_records"] == 1
    assert restored["paper"]["state"]["fills"] == 0


def test_failed_final_status_save_never_records_normal_exit(tmp_path, monkeypatch):
    original = service.atomic_write

    def fail_final(path, content):
        if path.name == "status.json" and json.loads(content).get("normal_exit") is True:
            raise OSError("injected final status failure")
        return original(path, content)

    monkeypatch.setattr(service, "atomic_write", fail_final)
    with pytest.raises(OSError):
        execute(tmp_path, [NOW], recover_only=True, observer=forbidden)
    records = [tmp_path / "status.json", *list((tmp_path / "sessions").glob("*.json"))]
    for p in records:
        value = json.loads(p.read_bytes())
        assert value["status"] == "failed" and value["normal_exit"] is False


def test_integrity_failure_prevents_normal_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "audit", lambda *_: {"issues": ["raw_hash_mismatch"]})
    with pytest.raises(ValueError, match="inconsistent"):
        execute(tmp_path, [NOW], recover_only=True, observer=forbidden)
    assert json.loads((tmp_path / "status.json").read_bytes())["normal_exit"] is False


def test_cli_recovery_never_requires_existing_workbook(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(service, "observe", forbidden)
    code = service.main(
        [
            "--root",
            str(tmp_path),
            "--workbook",
            str(tmp_path / "absent.xlsx"),
            "--code",
            CODE,
            "--paper",
            "--recover-only",
        ]
    )
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == 0 and result["normal_exit"]
    assert result["purpose"] == "postprocessing"


def test_full_audit_above_old_row_and_run_limits(tmp_path):
    # One validated source body replicated across distinct capture identities.
    run = tmp_path / "runs" / ("a" * 32)
    run.mkdir(parents=True)
    (run / "000.json").write_bytes(body())
    root = tmp_path / "store"
    with RSSStore(root) as store:
        store.import_run(run)
        store.db.execute("CREATE TEMP TABLE template_imports AS SELECT * FROM imports")
        store.db.execute("CREATE TEMP TABLE template_observations AS SELECT * FROM observations")
        store.db.execute("DELETE FROM imports")
        store.db.execute("DELETE FROM observations")
        store.db.execute(
            "INSERT INTO imports SELECT lpad(lower(to_hex(r)),32,'0'),s,"
            "raw_sha256,raw_path,imported_at "
            "FROM template_imports CROSS JOIN range(1,1002) r(r) CROSS JOIN range(10) s(s)"
        )
        store.db.execute(
            "INSERT INTO observations SELECT lpad(lower(to_hex(r)),32,'0'),s,"
            "o.* EXCLUDE(run_id,sample) "
            "FROM template_observations o CROSS JOIN range(1,1002) r(r) CROSS JOIN range(10) s(s)"
        )
        store.export()
    (run / "000.json").unlink()
    run.rmdir()
    summary = dict(
        schema_version=1, samples=10, excel_reads=10, attention_samples=0, status="observed"
    )
    for r in range(1, 1002):
        folder = tmp_path / "runs" / f"{r:032x}"
        folder.mkdir()
        for sample in range(10):
            (folder / f"{sample:03d}.json").write_bytes(body())
        (folder / "summary.json").write_text(json.dumps(summary))
    assert audit(root)["status"] == "consistent"
    coverage = audit_runs(root, tmp_path / "runs")
    assert coverage["status"] == "consistent"
    assert coverage["runs"] == 1001 and coverage["matched_samples"] == 10010
    # Corruption after the former 10,000th row must still be detected.
    import duckdb

    with duckdb.connect(str(root / "rss.duckdb")) as db:
        db.execute(
            "UPDATE observations SET price='999' WHERE run_id=? AND sample=9", [f"{1001:032x}"]
        )
    assert "raw_catalog_mismatch" in audit(root)["issues"]
    last = tmp_path / "runs" / f"{1001:032x}" / "009.json"
    last.write_bytes(last.read_bytes() + b" ")
    assert audit_runs(root, tmp_path / "runs")["changed_samples"] == 1
