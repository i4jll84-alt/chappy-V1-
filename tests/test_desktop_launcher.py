import json
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trading_system import desktop_launcher as launcher
from trading_system.brokers.locking import single_writer
from trading_system.data_sources.rss_snapshot import inspect_cache
from trading_system.tk_startup import create_window


def test_research_result_is_specific_fresh_and_never_claims_running(tmp_path):
    root = launcher.collector_root(tmp_path, "panel")
    root.mkdir(parents=True)
    at = datetime(2026, 11, 4, 11, 9, tzinfo=UTC)
    status = {
        "updated_at": at.isoformat(),
        "state": "failed",
        "reason": "research_workbook_formulas_changed",
    }
    (root / "status.json").write_text(json.dumps(status))
    assert "取得用数式" in launcher.research_result(tmp_path)
    assert launcher.research_result(tmp_path, int(at.timestamp() + 1) * 10**9) is None
    status.update(state="stopped", reason="one_pass_complete", chart_symbols=31)
    (root / "status.json").write_text(json.dumps(status))
    assert "保存完了（分足 31/31銘柄）" in launcher.research_result(tmp_path)
    assert "当日分足" not in launcher.research_result(tmp_path)
    status.update(current_day_chart_symbols=0, latest_chart_at="2026-11-03T15:30:00+09:00")
    for reason in ("daytime_1600", "one_pass_complete"):
        status["reason"] = reason
        (root / "status.json").write_text(json.dumps(status))
        assert "当日分足 0/31銘柄（最新 11/03 15:30）" in launcher.research_result(tmp_path)
    status.update(state="needs_review", streaming_charts=0, connection_required_charts=31)
    (root / "status.json").write_text(json.dumps(status))
    assert "未接続" in launcher.research_result(tmp_path)
    assert "RSS配信中 0/31" in launcher.research_result(tmp_path)
    status.update(
        state="stopped",
        reason="one_pass_recent_only",
        streaming_charts=31,
        connection_required_charts=0,
    )
    (root / "status.json").write_text(json.dumps(status))
    assert "当日分は全銘柄で揃っていません" in launcher.research_result(tmp_path)
    status.update(state="read")
    (root / "status.json").write_text(json.dumps(status))
    assert launcher.research_result(tmp_path) is None


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
    view.vendor_executor = Mock()
    done = Future()
    done.set_result(None)
    view.vendor_executor.submit.return_value = done
    view.vendor_tasks = {}
    view.start_notes = {}
    view.requested = {}
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
    message = " ".join(view.start_notes.values())
    if state == "rakuten_spawn_failed":
        assert set(view.children) == {"kabu_panel"} and "起動できません" in message
    elif state == "kabu_check_running":
        assert set(view.children) == {"panel", "mufg_esmart"} and "接続確認中" in message
    else:
        assert set(view.children) == {"panel", "kabu_panel"} and spawn.call_count == 1


def test_launcher_buttons_use_selection_for_research_and_connection(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import ttk

    check, study, daily = Mock(), Mock(), Mock()
    monkeypatch.setattr(launcher.Launcher, "start", check)
    monkeypatch.setattr(launcher.Launcher, "start_research", study)
    monkeypatch.setattr(launcher.Launcher, "start_daily_save", daily)
    monkeypatch.setattr(launcher.Launcher, "refresh", Mock())
    window = create_window(tk)
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
        evening_button = buttons["当日分を保存"]
        assert evening_button.master.cget("text") == "夜の作業（16:30以降）"
        evening_button.invoke()
        daily.assert_called_once()
        state_label = next(
            w for w in widgets if isinstance(w, ttk.Label) and w.cget("text") == "夜の当日分保存："
        )
        assert state_label.master.master.cget("text") == "稼働状態"
        assert any(
            str(w.cget("textvariable")) == str(view.daily_status)
            for w in state_label.master.winfo_children()
        )
    finally:
        window.destroy()


def daily_view(tmp_path):
    view = launcher.Launcher.__new__(launcher.Launcher)
    view.project = tmp_path
    view.daily_executor = Mock()
    view.daily_task = None
    view.daily_seen = None
    view.daily_root = tmp_path / "daily-snapshots"
    view.daily_status = Mock()
    view.window = Mock()
    return view


def test_daily_save_launch_time_double_click_and_close(tmp_path, monkeypatch):
    view = daily_view(tmp_path)
    edge = datetime.fromisoformat("2026-11-04T16:30:00+09:00")
    stamp = int(edge.timestamp()) * 10**9
    monkeypatch.setattr(launcher, "time_ns", lambda: stamp - 1)
    view.start_daily_save()
    view.daily_executor.submit.assert_not_called()
    assert "16:30以降" in view.daily_status.set.call_args.args[0]
    monkeypatch.setattr(launcher, "time_ns", lambda: stamp)
    task = Future()
    view.daily_executor.submit.return_value = task
    view.start_daily_save()
    view.start_daily_save()
    view.daily_executor.submit.assert_called_once_with(
        launcher.save_market_day, tmp_path, edge.date()
    )
    view.refresh_daily_save()
    assert "保存中" in view.daily_status.set.call_args.args[0]
    info = Mock()
    monkeypatch.setattr("tkinter.messagebox.showinfo", info)
    view.close()
    info.assert_called_once()
    view.window.destroy.assert_not_called()
    task.set_result({"state": "complete", "date": "2026-11-04", "daily_rows": 1234})
    view.refresh_daily_save()
    assert view.daily_task is None
    assert "保存完了（日足 1,234 銘柄）" in view.daily_status.set.call_args.args[0]


@pytest.mark.parametrize(
    "result,text",
    [
        ({"state": "closed_day", "date": "2026-11-03"}, "休場日"),
        (ValueError("daily_not_yet_available"), "未配信"),
        (ValueError("broker_state_already_open"), "別の保存処理"),
        (RuntimeError("private fixture details"), "保存に失敗"),
    ],
)
def test_daily_save_reports_outcome_and_allows_retry(tmp_path, result, text):
    view = daily_view(tmp_path)
    view.daily_task = Future()
    if isinstance(result, Exception):
        view.daily_task.set_exception(result)
    else:
        view.daily_task.set_result(result)
    view.refresh_daily_save()
    assert view.daily_task is None
    assert text in view.daily_status.set.call_args.args[0]
    assert "private fixture" not in view.daily_status.set.call_args.args[0]


def test_daily_state_follows_external_lock_and_current_date_report(tmp_path, monkeypatch):
    view = daily_view(tmp_path)
    now = datetime.fromisoformat("2026-11-04T18:00:00+09:00")
    stamp = int(now.timestamp()) * 10**9
    monkeypatch.setattr(launcher, "time_ns", lambda: stamp)
    view.refresh_daily_save()
    assert view.daily_status.set.call_args.args == ("待機中",)
    with single_writer(view.daily_root):
        view.refresh_daily_save()
        assert "保存中" in view.daily_status.set.call_args.args[0]
    view.refresh_daily_save()
    assert view.daily_status.set.call_args.args == ("待機中",)
    path = view.daily_root / "2026-11-04/last_collection.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"state": "complete", "date": "2026-11-04", "daily_rows": 12}))
    view.refresh_daily_save()
    assert "本日の保存記録あり" in view.daily_status.set.call_args.args[0]
    monkeypatch.setattr(launcher, "time_ns", lambda: stamp + 86400 * 10**9)
    view.refresh_daily_save()
    assert view.daily_status.set.call_args.args == ("待機中",)


def test_daily_button_calls_same_collector_with_project_settings(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/stage1.toml").write_text('data_root = "local-jquants"\n')
    collect = Mock(return_value={"state": "closed_day"})
    monkeypatch.setattr("trading_system.research.swing_daily.collect", collect)
    day = datetime(2026, 11, 3).date()
    assert launcher.save_market_day(tmp_path, day) == collect.return_value
    received_day, settings = collect.call_args.args
    assert received_day == day and settings.data_root == tmp_path / "local-jquants"


def test_kabu_research_lock_is_not_shown_as_a_running_connection_check(tmp_path):
    view = research_view(tmp_path, "both")
    view.window = Mock()
    view.status = {b: Mock() for b in launcher.CAPTURES}
    view.busy = lambda broker: broker in ("kabu_panel", "mufg_esmart")
    view.refresh_daily_save = Mock()
    view.refresh()
    assert "停止中" in view.status["mufg_esmart"].set.call_args.args[0]
    assert "起動中" in view.status["kabu_panel"].set.call_args.args[0]


def test_kabu_station_wait_has_separate_status_from_password_prompt(tmp_path):
    view = research_view(tmp_path, "both")
    view.window = Mock()
    view.status = {b: Mock() for b in launcher.CAPTURES}
    view.busy = lambda broker: broker == "kabu_panel"
    view.refresh_daily_save = Mock()
    folder = launcher.collector_root(tmp_path, "kabu_panel")
    folder.mkdir(parents=True)
    for state, message in [
        ("waiting_for_station", "API待ち"),
        ("prompt", "APIパスワード"),
    ]:
        (folder / "status.json").write_text(
            json.dumps({"state": state, "updated_at": datetime.now(UTC).isoformat()})
        )
        view.refresh()
        assert message in view.status["kabu_panel"].set.call_args.args[0]


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
        refresh_research_starts=Mock(),
        requested={},
        vendor_tasks={},
        start_notes={},
        refresh_daily_save=Mock(),
        busy=lambda broker: broker == "rakuten",
        status={b: Mock() for b in launcher.CAPTURES},
        rakuten_requested_ns=int(requested.timestamp()) * 1_000_000_000,
    )
    for started, expected in [
        ("2026-11-01T10:00:00+09:00", "保存完了を待っています"),
        (
            "2026-11-02T09:00:01+09:00",
            "停止要求済み / 過去データを復元中（1,250 / 9,399 組）",
        ),
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
    view = SimpleNamespace(
        project=tmp_path,
        stopping=set(),
        message=Mock(),
        vendor_tasks={},
        start_notes={},
    )
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
