import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trading_system import desktop_launcher as launcher
from trading_system.brokers.locking import single_writer
from trading_system.data_sources.rss_snapshot import inspect_cache


def settings():
    return {
        "brokers": ["rakuten"],
        "rakuten_app": "C:/Apps/楽天.lnk",
        "mufg_app": "C:/Apps/kabu.exe",
    }


def test_settings_roundtrip_has_only_non_secret_fields(tmp_path):
    path = tmp_path / "settings.json"
    launcher.save_settings(path, settings())
    assert json.loads(path.read_text("utf-8")) == settings()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("brokers", [[], ["unknown"], ["rakuten", "rakuten"], "rakuten", None])
def test_invalid_selection_rejected(brokers):
    with pytest.raises(ValueError):
        launcher.validate_settings({**settings(), "brokers": brokers})


@pytest.mark.parametrize("extra", ["password", "api_key", "live_trading_enabled", "endpoint"])
def test_settings_reject_secrets_and_live_options(extra):
    with pytest.raises(ValueError):
        launcher.validate_settings({**settings(), extra: "must-not-save"})


@pytest.mark.parametrize("path", ["https://example.test/", "C:/Apps/run.cmd", "C:/Apps/run.ps1"])
def test_app_paths_are_not_arbitrary_commands(path):
    with pytest.raises(ValueError):
        launcher.validate_settings({**settings(), "rakuten_app": path})


@pytest.mark.parametrize(
    "setting, filename, from_bin",
    [
        ("rakuten_app", "MarketSpeed2.exe", True),
        ("rakuten_app", "MarketSpeed2.EXE", True),
        ("rakuten_app", "MarketSpeed2.lnk", False),
        ("mufg_app", "KabuS.exe", False),
    ],
)
def test_vendor_launch_uses_rakuten_exe_directory_only(
    tmp_path, monkeypatch, setting, filename, from_bin
):
    monkeypatch.chdir(tmp_path)
    directory = Path("アプリ Bin")
    directory.mkdir()
    app = directory / filename
    app.touch()
    start = Mock()
    monkeypatch.setattr(launcher.os, "startfile", start, raising=False)
    launcher.Launcher.open_app(SimpleNamespace(settings={setting: str(app)}), setting)
    if from_bin:
        start.assert_called_once_with(app.absolute(), cwd=directory.absolute())
    else:
        start.assert_called_once_with(app)
    assert Path.cwd() == tmp_path


def test_commands_use_existing_bounded_collectors_and_fixed_local_roots(tmp_path):
    rss = launcher.launch_command(tmp_path, "rakuten")
    mufg = launcher.launch_command(tmp_path, "mufg_esmart")
    assert rss[-2:] == ["-Action", "Start"]
    assert str(tmp_path / "scripts/start_rss_continuous.ps1") in rss
    assert "trading_system.brokers.mufg_esmart.capture" in mufg
    assert "--prompt" in mufg and "--verify-reconnect" not in mufg
    assert mufg[-1] == str(tmp_path / "data/mufg-continuous")
    with pytest.raises(ValueError):
        launcher.launch_command(tmp_path, "other")


def test_stop_is_cooperative_and_existing_data_is_retained(tmp_path):
    root = launcher.collector_root(tmp_path, "rakuten")
    root.mkdir(parents=True)
    old = root / "status.json"
    old.write_text('{"status":"running","pid":1}', encoding="utf-8")
    launcher.request_stop(tmp_path, "rakuten")
    assert (root / "STOP").is_file()
    assert json.loads(old.read_text())["pid"] == 1
    assert not launcher.collector_active(tmp_path, "rakuten")


def test_mufg_lock_prevents_resume_and_stop_is_preserved(tmp_path):
    root = launcher.collector_root(tmp_path, "mufg_esmart")
    root.mkdir(parents=True)
    (root / "STOP").write_text("original stop")
    with single_writer(root):
        assert launcher.collector_active(tmp_path, "mufg_esmart")
        with pytest.raises(ValueError):
            launcher.archive_mufg_stop(tmp_path)
        assert (root / "STOP").read_text() == "original stop"
    assert not launcher.collector_active(tmp_path, "mufg_esmart")
    launcher.archive_mufg_stop(tmp_path)
    assert not (root / "STOP").exists()
    assert [p.read_text() for p in (root / "stop-history").iterdir()] == ["original stop"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("rakuten", {"panel": "research_panel"}),
        ("mufg_esmart", {"kabu_panel": "kabu_study"}),
        ("both", {"panel": "research_panel", "kabu_panel": "kabu_study"}),
    ],
)
@pytest.mark.parametrize("hour,once", [(9, False), (20, True)])
def test_selected_research_uses_click_time_and_remembers_choice(
    tmp_path, monkeypatch, mode, expected, hour, once
):
    when = datetime.fromisoformat(f"2026-11-04T{hour:02}:00:00+09:00")
    monkeypatch.setattr(launcher, "time_ns", lambda: int(when.timestamp() * 1e9))
    spawn = Mock()
    spawn.return_value.poll.return_value = None
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    view = research_view(tmp_path, mode)
    view.start_research()
    assert list(view.children) == list(expected)
    assert spawn.call_count == len(expected)
    for module, call in zip(expected.values(), spawn.call_args_list, strict=True):
        args = call.args[0]
        assert ("--snapshot-now" in args) == once and "trading_system." + module in args
        assert args[args.index("--requested") + 1] == str(launcher.time_ns())
    assert json.loads(view.settings_path.read_text("utf-8"))["brokers"] == view.selected()
    view.start_research()
    assert spawn.call_count == len(expected)


def research_view(tmp_path, mode):
    view = launcher.Launcher.__new__(launcher.Launcher)
    view.project = tmp_path
    view.settings_path = tmp_path / "settings.json"
    view.settings = settings()
    view.mode = Mock()
    view.mode.get.return_value = mode
    view.children = {}
    view.stopping = set()
    view.message = Mock()
    return view


@pytest.mark.parametrize(
    "state", ["rakuten_already_running", "kabu_check_running", "rakuten_spawn_failed"]
)
def test_both_research_reports_each_selected_result(tmp_path, monkeypatch, state):
    view = research_view(tmp_path, "both")
    if state == "rakuten_already_running":
        view.children["panel"] = Mock()
        view.children["panel"].poll.return_value = None
    if state == "kabu_check_running":
        view.children["mufg_esmart"] = Mock()
        view.children["mufg_esmart"].poll.return_value = None
    spawn = Mock()
    if state == "rakuten_spawn_failed":
        spawn.side_effect = [OSError("fixture"), Mock()]
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    view.start_research()
    message = view.message.set.call_args.args[0]
    assert all(launcher.NAMES[k] in message for k in launcher.RESEARCH.values())
    if state == "rakuten_spawn_failed":
        assert set(view.children) == {"kabu_panel"} and "起動できません" in message
    elif state == "kabu_check_running":
        assert set(view.children) == {"panel", "mufg_esmart"} and "接続確認中" in message
    else:
        assert set(view.children) == {"panel", "kabu_panel"} and "起動済み" in message


def test_launcher_buttons_use_selection_for_research_and_connection(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import ttk

    check, study = Mock(), Mock()
    monkeypatch.setattr(launcher.Launcher, "start", check)
    monkeypatch.setattr(launcher.Launcher, "start_research", study)
    monkeypatch.setattr(launcher.Launcher, "refresh", Mock())
    window = tk.Tk()
    window.withdraw()
    try:
        view = launcher.Launcher(window, tmp_path)
        widgets = [window]
        for widget in widgets:
            widgets.extend(widget.winfo_children())
        buttons = {w.cget("text"): w for w in widgets if isinstance(w, ttk.Button)}
        app_row = buttons["MARKET SPEED II"].master.pack_slaves()
        assert [b.cget("text") for b in app_row] == [
            "MARKET SPEED II",
            "kabuステーション",
            "楽天の接続確認用Excelを開く",
        ]
        assert all(str(b.pack_info()["side"]) == "left" for b in app_row)
        research_buttons = [label for label in buttons if "研究取得" in label]
        assert research_buttons == ["選択した証券会社の研究取得（31銘柄）"]
        assert "選択した証券会社を開始" not in buttons
        next(
            w for w in widgets if isinstance(w, ttk.Radiobutton) and w.cget("text") == "両方"
        ).invoke()
        assert view.selected() == list(launcher.BROKERS)
        buttons[research_buttons[0]].invoke()
        study.assert_called_once()
        check.assert_not_called()
        buttons["接続を確認（トヨタ1銘柄）"].invoke()
        check.assert_called_once()
    finally:
        window.destroy()


def test_kabu_research_lock_is_not_shown_as_a_running_connection_check(tmp_path):
    view = research_view(tmp_path, "both")
    view.window = Mock()
    view.status = {b: Mock() for b in launcher.CAPTURES}
    view.busy = lambda broker: broker in ("kabu_panel", "mufg_esmart")
    view.refresh()
    assert "停止中" in view.status["mufg_esmart"].set.call_args.args[0]
    assert "起動中" in view.status["kabu_panel"].set.call_args.args[0]


def test_refresh_reports_only_current_rakuten_recovery(tmp_path):
    root = launcher.collector_root(tmp_path, "rakuten")
    root.mkdir(parents=True)
    requested = datetime(2026, 11, 2, tzinfo=UTC)
    report = dict(phase="restoring", recovery_runs_checked=1250, recovery_runs_total=9399)
    child = Mock()
    child.poll.return_value = None
    view = SimpleNamespace(
        project=tmp_path,
        children={"rakuten": child},
        stopping={"rakuten"},
        window=Mock(),
        refresh=Mock(),
        busy=lambda broker: broker == "rakuten",
        status={b: Mock() for b in launcher.CAPTURES},
        rakuten_requested_ns=int(requested.timestamp()) * 1_000_000_000,
    )
    for started, expected in [
        ("2026-11-01T10:00:00+09:00", "保存完了を待っています"),
        ("2026-11-02T09:00:01+09:00", "停止要求済み / 過去データを復元中（1,250 / 9,399 組）"),
    ]:
        (root / "status.json").write_text(json.dumps(dict(report, started_at=started)))
        launcher.Launcher.refresh(view)
        assert expected in view.status["rakuten"].set.call_args.args[0]
    (root / "status.json").write_bytes(b"incomplete")
    launcher.Launcher.refresh(view)
    assert "保存完了を待っています" in view.status["rakuten"].set.call_args.args[0]


def test_both_selection_does_not_partially_launch_when_one_is_busy(tmp_path, monkeypatch):
    import tkinter.messagebox

    monkeypatch.setattr(tkinter.messagebox, "showinfo", Mock())
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    view = SimpleNamespace(
        selected=lambda: list(launcher.BROKERS),
        busy=lambda broker: broker == "mufg_esmart",
        project=tmp_path,
    )
    launcher.Launcher.start(view)
    spawn.assert_not_called()


def test_new_template_preserves_strict_three_cell_contract():
    project = Path(__file__).resolve().parents[1]
    workbook = project / "assets" / "rakuten-rss-template.xlsx"
    result = inspect_cache(workbook.read_bytes(), "7203.T", datetime.now(UTC))
    assert set(result["cells"]) == {"A1", "A2", "A3"}
    assert result["live_connection_verified"] is False
    assert result["freshness_verified"] is False
    assert all("excel_error" in issue or "missing_cache" in issue for issue in result["issues"])


@pytest.mark.parametrize(
    "failed", [(), ("rakuten",), ("mufg_esmart",), ("panel",), launcher.CAPTURES]
)
def test_stop_all_attempts_each_broker_on_independent_write_errors(tmp_path, monkeypatch, failed):
    calls = []

    def publish(project, broker):
        calls.append(broker)
        if broker in failed:
            raise PermissionError("unwritable")
        assert project == tmp_path

    showerror = Mock()
    monkeypatch.setattr(launcher, "request_stop", publish)
    monkeypatch.setattr("tkinter.messagebox.showerror", showerror)
    view = SimpleNamespace(project=tmp_path, stopping=set(), message=Mock())
    launcher.Launcher.stop_all(view)
    assert calls == list(launcher.CAPTURES)
    assert view.stopping == set(launcher.CAPTURES) - set(failed)
    if failed:
        showerror.assert_called_once()
        assert all(launcher.NAMES[k] in showerror.call_args.args[1] for k in failed)
        view.message.set.assert_not_called()
    else:
        showerror.assert_not_called()
        view.message.set.assert_called_once()
