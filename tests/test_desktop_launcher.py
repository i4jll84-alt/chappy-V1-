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
