"""Startup waiting acceptance tests with saved Raw and a virtual JST clock."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_rss_continuous import CODE, raw

from trading_system import rss_continuous as service
from trading_system.data_sources.rss_live import observe
from trading_system.rss_startup import assess_startup
from trading_system.runtime.models import JST
from trading_system.storage.rss import RSSStore


class Feed:
    def __init__(self, kinds, at=None):
        self.kinds = iter(kinds)
        self.now = at or datetime(2026, 1, 4, 23, 59, 50, tzinfo=UTC)
        self.reads = []
        self.sleep_hook = None

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)
        if self.sleep_hook:
            self.sleep_hook()

    def read(self, *_):
        kind = next(self.kinds)  # Unexpected extra reads fail the test.
        self.reads.append((self.now, kind))
        if kind == "read_error":
            return {"status": "error", "reason": "excel_not_ready"}
        value = raw(self.now)
        day = self.now.astimezone(JST).date()
        if kind == "past":
            day -= timedelta(days=3)  # Do not assume yesterday was a trading day.
        elif kind == "future":
            day += timedelta(days=1)
        for key in ("values_before", "values_after"):
            value[key] = list(value[key])
            value[key][2] = day.strftime("%Y/%m/%d")
            if kind == "bad_price":
                value[key][0] = "not a price"
        return value

    def observe(self, workbook, code, runs, **kwargs):
        return observe(
            workbook,
            code,
            runs,
            **kwargs,
            reader=self.read,
            utcnow=lambda: self.now,
            clock=lambda: self.now.timestamp(),
            sleeper=self.sleep,
        )

    def run(self, root, **kwargs):
        return service.run(
            workbook=root / "absent.xlsx",
            root=root,
            code=CODE,
            paper=True,
            wait_for_current_date=True,
            cycles=1,
            observer=self.observe,
            utcnow=lambda: self.now,
            sleeper=self.sleep,
            **kwargs,
        )


def test_prior_day_waits_then_captures_without_manual_restart(tmp_path):
    feed = Feed(["past"] * 3 + ["today"] * 8)
    progress = []

    def record(event):
        if event["phase"] in ("waiting_for_current_date", "capturing"):
            saved = json.loads((tmp_path / "status.json").read_bytes())
            progress.append((event["phase"], saved["paper"]["state"]["fills"]))

    result = feed.run(tmp_path, progress=record)
    assert progress == [("waiting_for_current_date", 0), ("capturing", 0)]
    assert (feed.reads[3][0] - feed.reads[2][0]).total_seconds() == pytest.approx(30)
    assert result["startup_attempts"] == 2 and result["startup_samples"] == 6
    assert result["startup_status"] == "ready" and result["cycles_completed"] == 1
    assert result["samples"] == result["audit"]["observations"] == 11
    assert result["attention_samples"] == 3 and result["normal_exit"]
    assert result["coverage"]["unimported_samples"] == 0
    assert result["paper"]["accounting_matches"] and not result["real_execution_enabled"]
    assert not result["freshness_verified"]
    records = [json.loads(p.read_bytes()) for p in (tmp_path / "runs").glob("*/*.json")]
    stale = [r for r in records if r.get("issues") == ["current_date_not_today"]]
    assert len(stale) == 3 and all(r["status"] == "attention_required" for r in stale)


def test_today_at_first_check_proceeds_without_wait(tmp_path):
    result = Feed(["today"] * 8).run(tmp_path)
    assert result["startup_attempts"] == 1 and result["startup_samples"] == 3
    assert result["samples"] == 8 and result["attention_samples"] == 0
    assert result["startup_status"] == "ready" and result["normal_exit"]


def test_mixed_dates_require_a_fully_current_batch(tmp_path):
    result = Feed(["past", "today", "today"] + ["today"] * 8).run(tmp_path)
    assert result["startup_attempts"] == 2 and result["attention_samples"] == 1
    assert result["startup_samples"] == 6 and result["samples"] == 11


def test_stop_button_during_wait_saves_without_any_fill(tmp_path):
    feed = Feed(["past"] * 3)

    def stop(event):
        if event["phase"] == "waiting_for_current_date":
            (tmp_path / "STOP").touch()

    result = feed.run(tmp_path, progress=stop)
    assert result["stop_reason"] == "stop_file" and result["normal_exit"]
    assert result["samples"] == result["export"]["tables"]["observations"]["rows"] == 3
    assert result["cycles_completed"] == 0 and result["paper"]["state"]["fills"] == 0


def test_1600_during_wait_does_not_start_another_read(tmp_path):
    feed = Feed(["past"] * 3, datetime(2026, 1, 5, 6, 59, 50, tzinfo=UTC))
    result = feed.run(tmp_path)
    assert result["stop_reason"] == "daytime_1600" and result["normal_exit"]
    assert result["samples"] == 3 and result["cycles_completed"] == 0
    assert result["paper"]["state"]["fills"] == 0


def test_sleep_until_next_day_keeps_original_deadline(tmp_path):
    feed = Feed(["past"] * 3)

    def sleep_hook():
        saved = json.loads((tmp_path / "status.json").read_bytes())
        if saved["phase"] == "waiting_for_current_date":
            feed.now += timedelta(days=1)

    feed.sleep_hook = sleep_hook
    result = feed.run(tmp_path)
    assert result["scheduled_stop_at"] == "2026-01-05T16:00:00+09:00"
    assert result["stop_reason"] == "daytime_1600" and len(feed.reads) == 3


def test_deadline_mid_probe_preserves_partial_batch(tmp_path):
    feed = Feed(["past"], datetime(2026, 1, 5, 6, 59, 59, tzinfo=UTC))
    result = feed.run(tmp_path)
    assert result["normal_exit"] and result["stop_reason"] == "daytime_1600"
    assert result["samples"] == result["audit"]["observations"] == 1
    assert result["startup_status"] != "ready"


@pytest.mark.parametrize("kind", ["future", "bad_price", "read_error"])
def test_other_errors_block_and_preserve_saved_data(tmp_path, kind):
    with pytest.raises(ValueError, match="rss_startup_check_failed"):
        Feed([kind] * 3).run(tmp_path)
    result = json.loads((tmp_path / "status.json").read_bytes())
    assert result["status"] == "failed" and not result["normal_exit"]
    assert result["startup_status"] == "blocked" and result["startup_attempts"] == 1
    assert result["stop_reason"] == "startup_check_failed"
    assert result["export"]["tables"]["observations"]["rows"] == 3
    assert result["coverage"]["unimported_samples"] == 0
    assert result["paper"]["state"]["fills"] == 0


def test_past_date_does_not_hide_another_failure(tmp_path):
    with pytest.raises(ValueError, match="rss_startup_check_failed"):
        Feed(["past", "bad_price", "past"]).run(tmp_path)
    result = json.loads((tmp_path / "status.json").read_bytes())
    assert result["startup_status"] == "blocked"
    assert set(result["startup_issues"]) == {"current_date_not_today", "invalid_price"}


def test_wait_holds_single_writer_lock(tmp_path):
    def progress(event):
        if event["phase"] == "waiting_for_current_date":
            with pytest.raises(ValueError, match="collector_already_running"):
                with service.single_writer(tmp_path):
                    pytest.fail("A second collector acquired the waiting collector's lock")
            (tmp_path / "STOP").touch()

    assert Feed(["past"] * 3).run(tmp_path, progress=progress)["normal_exit"]


def test_keyboard_interrupt_while_waiting_flushes_and_exits(tmp_path):
    def interrupt(event):
        if event["phase"] == "waiting_for_current_date":
            raise KeyboardInterrupt

    result = Feed(["past"] * 3).run(tmp_path, progress=interrupt)
    assert result["stop_reason"] == "keyboard_interrupt" and result["normal_exit"]
    assert result["coverage"]["unimported_samples"] == 0
    assert result["paper"]["state"]["fills"] == 0


def test_waiting_export_failure_is_not_normal_exit(tmp_path, monkeypatch):
    def fail(_):
        raise OSError("test export failure")

    monkeypatch.setattr(RSSStore, "export", fail)
    with pytest.raises(OSError, match="test export failure"):
        Feed(["past"] * 3).run(tmp_path, export_every=1)
    result = json.loads((tmp_path / "status.json").read_bytes())
    assert result["status"] == "failed" and not result["normal_exit"]
    assert len(list((tmp_path / "runs").glob("*/[0-9][0-9][0-9].json"))) == 3


@pytest.mark.parametrize("recover_only", [False, True])
def test_evening_or_recovery_skips_all_startup_reads(tmp_path, recover_only):
    at = datetime(2026, 1, 5, 10 if not recover_only else 1, tzinfo=UTC)
    feed = Feed([], at)
    result = feed.run(tmp_path, recover_only=recover_only)
    assert result["purpose"] == "postprocessing" and result["normal_exit"]
    assert not feed.reads and result["startup_attempts"] == 0


def test_saved_normalized_fields_cannot_bypass_raw_validation(tmp_path):
    feed = Feed(["past"] * 3)
    folder, _ = feed.observe(tmp_path / "x.xlsx", CODE, tmp_path, samples=3)
    for path in folder.glob("[0-9][0-9][0-9].json"):
        record = json.loads(path.read_bytes())
        record.update(status="observed", issues=[], freshness_verified=True)
        record["normalized"]["current_date"] = feed.now.astimezone(JST).date().isoformat()
        path.write_text(json.dumps(record), encoding="utf-8")
    assert assess_startup(folder, CODE, feed.now)["state"] == "waiting_for_current_date"


def test_current_day_in_record_is_not_enough_after_date_rollover(tmp_path):
    feed = Feed(["today"] * 3)
    folder, _ = feed.observe(tmp_path / "x.xlsx", CODE, tmp_path, samples=3)
    result = assess_startup(folder, CODE, feed.now + timedelta(days=1))
    assert result == {"state": "blocked", "issues": ["startup_date_changed"]}


def test_partial_batch_cannot_release_capture(tmp_path):
    feed = Feed(["today"] * 2)
    folder, _ = feed.observe(tmp_path / "x.xlsx", CODE, tmp_path, samples=2)
    assert assess_startup(folder, CODE, feed.now) == {
        "state": "blocked",
        "issues": ["startup_sample_count"],
    }


def test_cli_enables_startup_gate(tmp_path, monkeypatch, capsys):
    received = {}

    def run(**kwargs):
        received.update(kwargs)
        raise ValueError("No real capture in CLI wiring test")

    monkeypatch.setattr(service, "run", run)
    code = service.main(
        ["--workbook", str(tmp_path / "x.xlsx"), "--root", str(tmp_path), "--wait-for-current-date"]
    )
    assert code == 2 and received["wait_for_current_date"] is True
    assert json.loads(capsys.readouterr().out)["normal_exit"] is False
