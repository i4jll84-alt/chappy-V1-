"""Exercise the launcher and continuous MUFG prompt with no Excel or broker transport."""

import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trading_system import desktop_launcher as gui
from trading_system.brokers import market_data
from trading_system.brokers.locking import single_writer
from trading_system.brokers.mufg_esmart import capture


def instant(value):
    return datetime.fromisoformat(value)


def ns(value):
    return int(instant(value).timestamp()) * 1_000_000_000


def view(tmp_path, selected):
    screen = SimpleNamespace(
        project=tmp_path,
        settings=gui.default_settings(),
        settings_path=tmp_path / "settings.json",
        selected=lambda: list(selected),
        busy=lambda _: False,
        workbook=tmp_path / "fixture.xlsx",
        children={},
        stopping=set(),
        message=Mock(),
    )
    screen.open_workbook = lambda cutoff: gui.Launcher.open_workbook(screen, cutoff)
    return screen


@pytest.mark.parametrize(
    "at,exists,opens,starts",
    [
        ("2026-11-02T15:58:59+09:00", True, 1, 1),
        ("2026-11-02T15:58:59+09:00", False, 0, 0),
        ("2026-11-02T15:59:00+09:00", False, 0, 1),
        ("2026-11-02T15:59:59+09:00", True, 0, 1),
        ("2026-11-02T07:00:00+00:00", False, 0, 1),
        ("2026-11-02T16:01:00+09:00", True, 0, 1),
    ],
)
def test_rakuten_workbook_and_launch_boundary(tmp_path, monkeypatch, at, exists, opens, starts):
    screen = view(tmp_path, ["rakuten"])
    if exists:
        screen.workbook.touch()
    shell_open, child, error = Mock(), Mock(), Mock()
    monkeypatch.setattr(gui, "time_ns", lambda: ns(at))
    monkeypatch.setattr(gui.os, "startfile", shell_open, raising=False)
    monkeypatch.setattr(gui.subprocess, "Popen", child)
    monkeypatch.setattr("tkinter.messagebox.showerror", error)
    gui.Launcher.start(screen)
    assert shell_open.call_count == opens
    assert child.call_count == starts
    assert error.call_count == 1 - starts
    if starts:
        command = child.call_args.args[0]
        cutoff = command[command.index("-RecoveryCutoffUnix") + 1]
        assert int(cutoff) == int(instant("2026-11-02T15:59:00+09:00").timestamp())


@pytest.mark.parametrize("at", ["2026-11-02T16:00:00+09:00", "2026-11-03T09:00:00+09:00"])
def test_mufg_resume_confirmation_crossing_rakuten_cutoff(tmp_path, monkeypatch, at):
    screen = view(tmp_path, ["rakuten", "mufg_esmart"])
    screen.workbook.touch()
    gui.request_stop(tmp_path, "mufg_esmart")
    clock = [ns("2026-11-02T15:58:59+09:00")]

    def confirm(*_):
        clock[0] = ns(at)
        return True

    monkeypatch.setattr(gui, "time_ns", lambda: clock[0])
    monkeypatch.setattr("tkinter.messagebox.askyesno", confirm)
    monkeypatch.setattr(gui.os, "startfile", Mock(side_effect=AssertionError), raising=False)
    child = Mock()
    monkeypatch.setattr(gui.subprocess, "Popen", child)
    gui.Launcher.start(screen)
    assert set(screen.children) == {"rakuten", "mufg_esmart"}
    command = child.call_args_list[0].args[0]
    assert (
        int(command[command.index("-RecoveryCutoffUnix") + 1])
        == ns("2026-11-02T15:59:00+09:00") // 1_000_000_000
    )


def test_workbook_button_waits_until_next_day(tmp_path, monkeypatch):
    screen = view(tmp_path, ["rakuten"])
    screen.workbook.touch()
    shell_open = Mock()
    monkeypatch.setattr(gui.os, "startfile", shell_open, raising=False)
    clock = [ns("2026-11-02T16:00:00+09:00")]
    monkeypatch.setattr(gui, "time_ns", lambda: clock[0])
    assert gui.Launcher.open_workbook(screen) is None
    shell_open.assert_not_called()
    clock[0] = ns("2026-11-03T09:00:00+09:00")
    assert gui.Launcher.open_workbook(screen)
    shell_open.assert_called_once_with(screen.workbook)


def test_elapsed_cutoff_between_preflight_and_native_open(tmp_path, monkeypatch):
    screen = view(tmp_path, ["rakuten"])
    screen.workbook.touch()
    start = ns("2026-11-02T15:58:59+09:00")
    cutoff = gui.rss_cutoff(start)
    monkeypatch.setattr(gui, "time_ns", lambda: ns("2026-11-02T15:59:00+09:00"))
    monkeypatch.setattr(gui.os, "startfile", Mock(side_effect=AssertionError), raising=False)
    assert gui.Launcher.open_workbook(screen, cutoff) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher dialog")
@pytest.mark.parametrize(
    "event,reason",
    [
        ("deadline", "daytime_1600"),
        ("overnight", "daytime_1600"),
        ("stop", "operator_stop"),
        ("duration", "duration_complete"),
        ("cancel", "prompt_cancelled"),
        ("submit_at_stop", "operator_stop"),
        ("submit_at_cutoff", "daytime_1600"),
    ],
)
def test_real_hidden_tk_prompt_closes_and_releases_lock(
    tmp_path, monkeypatch, capsys, event, reason
):
    import tkinter as tk
    from tkinter import ttk

    clock = [instant("2026-11-02T15:59:59+09:00")]
    root = gui.collector_root(tmp_path, "mufg_esmart")
    make_window = tk.Tk
    windows, watchdog, callbacks = [], [], []

    def widgets(widget):
        for child in widget.winfo_children():
            yield child
            yield from widgets(child)

    def trigger(window):
        callbacks.append(event)
        assert all(e.cget("show") == "*" for e in widgets(window) if isinstance(e, ttk.Entry))
        with pytest.raises(ValueError):
            with single_writer(root):
                pass
        if event in ("deadline", "submit_at_cutoff"):
            clock[0] += timedelta(seconds=1)
        elif event == "overnight":
            clock[0] += timedelta(hours=17)
        elif event == "duration":
            clock[0] += timedelta(seconds=1)
        elif event in ("stop", "submit_at_stop"):
            gui.request_stop(tmp_path, "mufg_esmart")
        if event in ("cancel", "submit_at_stop", "submit_at_cutoff"):
            text = "取消" if event == "cancel" else "開始"
            next(
                b for b in widgets(window) if isinstance(b, ttk.Button) and b.cget("text") == text
            ).invoke()

    def hidden_tk():
        window = make_window()
        window.withdraw()
        windows.append(window)
        # Keep the real Tk timers and controls. The fixture never displays a window.
        window.deiconify = lambda: None
        window.after(10, trigger, window)

        def timeout():
            watchdog.append(True)
            next(
                b for b in widgets(window) if isinstance(b, ttk.Button) and b.cget("text") == "取消"
            ).invoke()

        window.after(4000, timeout)
        return window

    if event == "duration":
        clock[0] = instant("2026-11-02T10:00:00+09:00")
    monkeypatch.setattr(tk, "Tk", hidden_tk)
    client = Mock(side_effect=AssertionError("unexpected client construction"))
    monkeypatch.setattr(capture, "Client", client)
    monkeypatch.setattr(
        capture, "run", lambda *args, **kw: market_data.run(*args, **kw, clock=lambda: clock[0])
    )
    args = ["--root", str(root), "--prompt"]
    if event == "duration":
        args.extend(["--max-seconds", "1"])
    assert capture.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(windows) == 1 and callbacks == [event] and not watchdog
    assert result["stop_reason"] == reason and result["samples"] == 0
    assert (
        result["normal_exit"]
        and result["data_consistency_verified"]
        and result["order_requests"] == 0
    )
    assert json.loads((root / "status.json").read_bytes())["status"] == "stopped"
    assert len(list((root / "sessions").glob("*.json"))) == 1
    client.assert_not_called()
    with single_writer(root):
        pass


@pytest.mark.parametrize("script", ["init.tcl", "tk.tcl"])
def test_prompt_recovers_from_transient_library_read(monkeypatch, script):
    import tkinter as tk

    window = object()
    create = Mock(side_effect=[tk.TclError(f"Can't find a usable {script}"), window])
    monkeypatch.setattr(tk, "Tk", create)
    delay = Mock()
    monkeypatch.setattr(capture, "sleep", delay)
    assert capture._prompt_window(tk, lambda: None) is window
    delay.assert_called_once_with(0.05)
    assert create.call_count == 2


@pytest.mark.parametrize("reason", ["operator_stop", "daytime_1600"])
def test_stop_during_tk_retry_prevents_another_window(monkeypatch, reason):
    import tkinter as tk

    pending_stop = []
    create = Mock(side_effect=tk.TclError("Can't find a usable tk.tcl"))
    monkeypatch.setattr(tk, "Tk", create)
    monkeypatch.setattr(capture, "sleep", lambda _: pending_stop.append(reason))
    assert capture._prompt_window(tk, lambda: next(iter(pending_stop), None)) is None
    create.assert_called_once()


@pytest.mark.parametrize(
    "message,attempts", [("Can't find a usable tk.tcl", 3), ("other Tk failure", 1)]
)
def test_persistent_or_unrelated_tk_error_is_reported(monkeypatch, message, attempts):
    import tkinter as tk

    create = Mock(side_effect=tk.TclError(message))
    monkeypatch.setattr(tk, "Tk", create)
    delay = Mock()
    monkeypatch.setattr(capture, "sleep", delay)
    with pytest.raises(tk.TclError, match=message):
        capture._prompt_window(tk, lambda: None)
    assert create.call_count == attempts and delay.call_count == attempts - 1
