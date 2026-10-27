"""Research monitoring CLI rejects ambiguous JSON and protects saved experiments."""

import json

import pytest

from trading_system.cli import main


def args(source):
    return [
        "monitor-research",
        "--result",
        str(source),
        "--max-mse-ratio",
        "1.2",
        "--max-mean-shift",
        "1",
        "--minimum-rows",
        "20",
        "--observed-at",
        "2026-10-23T01:00:00+00:00",
    ]


def test_missing_result_does_not_create_research_directory(tmp_path, capsys):
    source = tmp_path / "missing" / "result.json"
    output = tmp_path / "report.json"
    assert main(args(source) + ["--output", str(output)]) == 2
    assert not source.parent.exists() and not output.exists()
    assert json.loads(capsys.readouterr().out)["message"].startswith("Operation failed")


@pytest.mark.parametrize(
    "payload",
    [
        '{"secret_marker":1,"secret_marker":2}',
        '{"nested":{"secret_marker":1,"secret_marker":2}}',
        '{"secret_marker":NaN}',
        '{"secret_marker":Infinity}',
        '{"secret_marker":-Infinity}',
        "{invalid_secret_marker",
    ],
)
def test_invalid_json_not_passed_to_monitor(tmp_path, monkeypatch, capsys, payload):
    source = tmp_path / "input" / "result.json"
    source.parent.mkdir()
    source.write_text(payload, encoding="utf-8")
    called = []
    monkeypatch.setattr(
        "trading_system.monitoring.research.monitor_research", lambda *a, **k: called.append(True)
    )
    output = tmp_path / "report.json"
    assert main(args(source) + ["--output", str(output)]) == 2
    assert not called and not output.exists()
    assert source.read_text() == payload
    assert "secret_marker" not in capsys.readouterr().out


@pytest.mark.parametrize("output_name", ["result.json", "another.json", "result.sqlite"])
def test_output_protection_preserves_research_files(tmp_path, capsys, output_name):
    source = tmp_path / "experiment" / "result.json"
    source.parent.mkdir()
    source.write_text('{"status":"saved"}', encoding="utf-8")
    before = source.read_bytes()
    assert main(args(source) + ["--output", str(source.parent / output_name)]) == 2
    assert source.read_bytes() == before
    assert len(list(source.parent.iterdir())) == 1
    capsys.readouterr()


@pytest.mark.parametrize("status,exit_code", [("observed", 0), ("attention", 3)])
def test_cli_passes_explicit_policy_and_preserves_source(
    tmp_path, monkeypatch, capsys, status, exit_code
):
    source = tmp_path / "experiment" / "result.json"
    source.parent.mkdir()
    source.write_text('{"status":"saved"}', encoding="utf-8")
    before = source.read_bytes()

    def inspect(value, **policy):
        assert value == {"status": "saved"}
        assert policy["now"].utcoffset().total_seconds() == 0
        assert policy["max_mse_ratio"] == 1.2
        assert policy["max_mean_shift"] == 1
        assert policy["minimum_rows"] == 20
        return {"status": status, "deployment_enabled": False}

    monkeypatch.setattr("trading_system.monitoring.research.monitor_research", inspect)
    output = tmp_path / "report.json"
    assert main(args(source) + ["--output", str(output)]) == exit_code
    assert json.loads(capsys.readouterr().out) == json.loads(output.read_text())
    assert source.read_bytes() == before
