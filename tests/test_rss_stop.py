"""Regression cases for acknowledging a specific stop across the GUI/collector handoff."""

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trading_system import desktop_launcher, rss_continuous, rss_stop


def test_same_timestamp_new_request_has_a_distinct_ticket(tmp_path):
    root = desktop_launcher.collector_root(tmp_path, "rakuten")
    desktop_launcher.request_stop(tmp_path, "rakuten")
    stop = root / "STOP"
    stamp = stop.stat().st_mtime_ns
    first = rss_stop.stop_ticket(stop, stamp + 1)
    assert rss_stop.stop_ticket(stop, stamp) == "none"
    desktop_launcher.request_stop(tmp_path, "rakuten")
    os.utime(stop, ns=(stamp, stamp))
    assert not rss_stop.can_start(root, first)
    assert stop.exists()
    assert rss_stop.can_start(root, rss_stop.stop_ticket(stop, stamp + 1))
    assert not stop.exists()


@pytest.mark.parametrize("when", ["before_rename", "after_rename", "both"])
def test_stop_overlapping_rename_is_durable(tmp_path, monkeypatch, when):
    stop = tmp_path / "STOP"
    stop.write_text("legacy stop")
    ticket = rss_stop.stop_ticket(stop)
    rename = Path.rename

    def racing_rename(self, dest):
        if when in {"before_rename", "both"}:
            self.write_text("first new request")
        result = rename(self, dest)
        if when in {"after_rename", "both"}:
            self.write_text("latest new request")
        return result

    with monkeypatch.context() as m:
        m.setattr(Path, "rename", racing_rename)
        assert not rss_stop.can_start(tmp_path, ticket)
    expected = "first new request" if when == "before_rename" else "latest new request"
    assert stop.read_text() == expected
    assert not rss_stop.can_start(tmp_path, ticket)
    assert rss_stop.can_start(tmp_path, rss_stop.stop_ticket(stop))


@pytest.mark.parametrize("publish_new", [False, True])
def test_crash_after_claim_keeps_stop_for_default_and_stale_start(
    tmp_path, monkeypatch, publish_new
):
    stop = tmp_path / "STOP"
    stop.write_text("old")
    ticket = rss_stop.stop_ticket(stop)
    rename = Path.rename

    def interrupted(self, dest):
        self.write_text("concurrent")
        rename(self, dest)
        if publish_new:
            self.write_text("newer")
        raise SystemExit

    with monkeypatch.context() as m:
        m.setattr(Path, "rename", interrupted)
        with pytest.raises(SystemExit):
            rss_stop.can_start(tmp_path, ticket)
    assert (tmp_path / "STOP.resuming").exists()
    assert not rss_stop.can_start(tmp_path, None)
    assert stop.read_text() == ("newer" if publish_new else "concurrent")
    assert not rss_stop.can_start(tmp_path, ticket)
    assert rss_stop.can_start(tmp_path, rss_stop.stop_ticket(stop))


def test_failed_restore_never_permits_daytime_read(tmp_path, monkeypatch):
    (tmp_path / "STOP.resuming").write_text("interrupted")
    with monkeypatch.context() as m:
        m.setattr(os, "rename" if os.name == "nt" else "link", Mock(side_effect=PermissionError))
        with pytest.raises(PermissionError):
            rss_stop.can_start(tmp_path, "none")
    assert (tmp_path / "STOP.resuming").read_text() == "interrupted"
    assert not rss_stop.can_start(tmp_path, "none")
    assert (tmp_path / "STOP").read_text() == "interrupted"


def test_gui_passes_click_time_snapshot_even_if_stop_arrives_during_workbook_open(
    tmp_path, monkeypatch
):
    root = desktop_launcher.collector_root(tmp_path, "rakuten")
    root.mkdir(parents=True)
    stop = root / "STOP"
    stop.write_text("old")
    # Exercise the daytime workbook-open path regardless of the actual test time.
    stamp = int(datetime(2026, 1, 5, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
    os.utime(stop, ns=(stamp, stamp))
    ticket = rss_stop.stop_ticket(stop)
    monkeypatch.setattr(desktop_launcher, "time_ns", lambda: stamp + 1)
    workbook = tmp_path / "template.xlsx"
    workbook.touch()
    view = SimpleNamespace(
        selected=lambda: ["rakuten"],
        busy=lambda _: False,
        project=tmp_path,
        settings={"brokers": ["rakuten"], "rakuten_app": "", "mufg_app": ""},
        settings_path=tmp_path / "settings.json",
        workbook=workbook,
        open_workbook=lambda cutoff: stop.write_text("new") or True,
        children={},
        stopping=set(),
        message=Mock(),
    )
    monkeypatch.setattr("tkinter.messagebox.showerror", pytest.fail)
    spawn = Mock()
    monkeypatch.setattr(desktop_launcher.subprocess, "Popen", spawn)
    desktop_launcher.Launcher.start(view)
    args = spawn.call_args.args[0]
    assert args[args.index("-ResumeStop") + 1] == ticket
    assert not rss_stop.can_start(root, ticket)
    assert stop.read_text() == "new"


def test_running_collector_does_not_lose_stop_to_a_second_start(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("old")
    ticket = rss_stop.stop_ticket(stop)
    kwargs = dict(
        root=tmp_path,
        workbook=tmp_path / "unused",
        code="7203.T",
        paper=False,
        utcnow=lambda: datetime(2026, 1, 5, 1, tzinfo=UTC),
    )
    with rss_continuous.single_writer(tmp_path):
        with pytest.raises(ValueError, match="collector_already_running"):
            rss_continuous.run(
                **kwargs, resume_stop=ticket, observer=Mock(side_effect=AssertionError)
            )
    assert stop.read_text() == "old"


def test_explicit_resume_then_stop_without_any_external_reader(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("old")
    ticket = rss_stop.stop_ticket(stop)
    calls = []

    def observer(*args, stop_requested, **kwargs):
        calls.append(1)
        stop.write_text("new stop")
        assert stop_requested()
        return None, {}

    kwargs = dict(
        root=tmp_path,
        workbook=tmp_path / "unused",
        code="7203.T",
        observer=observer,
        utcnow=lambda: datetime(2026, 1, 5, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="stop_requested"):
        rss_continuous.run(**kwargs)
    assert not calls
    report = rss_continuous.run(**kwargs, resume_stop=ticket)
    assert calls == [1]
    assert report["normal_exit"] and report["stop_reason"] == "stop_file"
    assert report["excel_reads"] == 0
    assert stop.read_text() == "new stop"


@pytest.mark.parametrize("failures", [2, 51])
def test_stop_publisher_retries_contention_and_reports_exhaustion(tmp_path, monkeypatch, failures):
    write = Path.write_text
    tokens = []

    def fail_while_locked(path, token, **kwargs):
        tokens.append(token)
        if len(tokens) <= failures:
            raise PermissionError("locked")
        return write(path, token, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_while_locked)
    monkeypatch.setattr(rss_stop, "sleep", lambda _: None)
    if failures == 51:
        with pytest.raises(PermissionError):
            rss_stop.publish_stop(tmp_path)
        assert len(tokens) == 51
    else:
        rss_stop.publish_stop(tmp_path)
        assert (tmp_path / "STOP").read_text() == tokens[-1]
        assert len(tokens) == 3
    assert len(set(tokens)) == 1
