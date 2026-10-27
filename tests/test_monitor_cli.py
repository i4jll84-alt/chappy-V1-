"""The monitor CLI must not overwrite or initialize execution state."""

import json
import subprocess
import sys

import pytest

from trading_system.cli import main
from trading_system.execution.demo import NOW, run_mock_demo


def test_missing_monitor_database_is_not_initialized(tmp_path, capsys):
    source = tmp_path / "missing" / "manager.sqlite"
    output = tmp_path / "report.json"
    assert main(["monitor-mock", "--database", str(source), "--output", str(output)]) == 2
    assert not source.parent.exists()
    assert not output.exists()
    assert json.loads(capsys.readouterr().out)["message"].startswith("Operation failed")


@pytest.mark.parametrize("name", ["manager.sqlite", "broker.sqlite", "report.json"])
def test_monitor_refuses_output_in_state_directory(tmp_path, capsys, name):
    root = tmp_path / "state"
    run_mock_demo(root)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert (
        main(
            [
                "monitor-mock",
                "--database",
                str(root / "manager.sqlite"),
                "--output",
                str(root / name),
            ]
        )
        == 2
    )
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    capsys.readouterr()


def test_monitor_cli_reports_halt_and_preserves_database(tmp_path, capsys):
    root = tmp_path / "state"
    run_mock_demo(root)
    source = root / "manager.sqlite"
    before = source.read_bytes()
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "monitor-mock",
                "--database",
                str(source),
                "--observed-at",
                NOW.isoformat(),
                "--output",
                str(output),
            ]
        )
        == 3
    )
    result = json.loads(capsys.readouterr().out)
    assert result == json.loads(output.read_text())
    assert result["status"] == "attention"
    assert result["real_execution_enabled"] is False
    assert source.read_bytes() == before


def test_monitor_cli_rejects_naive_clock_without_output(tmp_path, capsys):
    root = tmp_path / "state"
    run_mock_demo(root)
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "monitor-mock",
                "--database",
                str(root / "manager.sqlite"),
                "--observed-at",
                "2026-01-05T01:00:00",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert not output.exists()
    capsys.readouterr()


def test_separate_monitor_processes_do_not_modify_or_recover_state(tmp_path):
    root = tmp_path / "state"
    run_mock_demo(root)
    source = root / "manager.sqlite"
    before = {name: (root / name).read_bytes() for name in ("manager.sqlite", "broker.sqlite")}
    command = [
        sys.executable,
        "-m",
        "trading_system.cli",
        "monitor-mock",
        "--database",
        str(source),
        "--observed-at",
        NOW.isoformat(),
    ]
    results = [
        subprocess.run(command, capture_output=True, text=True, timeout=30) for _ in range(2)
    ]
    assert [result.returncode for result in results] == [3, 3]
    assert all(not result.stderr for result in results)
    assert json.loads(results[0].stdout) == json.loads(results[1].stdout)
    assert {name: (root / name).read_bytes() for name in before} == before
