import json

import pytest
from pydantic import SecretStr

from trading_system import cli


@pytest.mark.parametrize(
    "command,flags,dataset,params",
    [
        (
            "fetch-master",
            ["--date", "2026-10-22", "--code", "8697"],
            "master",
            {"date": "2026-10-22", "code": "86970"},
        ),
        (
            "fetch-calendar",
            ["--from", "2026-10-01", "--to", "2026-10-22"],
            "calendar",
            {"from": "2026-10-01", "to": "2026-10-22"},
        ),
    ],
)
def test_cli_reference_routes_without_opening_price_store(
    monkeypatch, tmp_path, capsys, command, flags, dataset, params
):
    monkeypatch.setattr(cli, "api_key", lambda: SecretStr("local-test-secret"))

    def acquire(root, client, actual_dataset, actual_params, plan, refresh):
        assert root == tmp_path
        assert client.endpoint == actual_dataset == dataset
        assert actual_params == params
        assert plan == "standard" and refresh
        return {"state": "complete", "rows_received": 1}

    monkeypatch.setattr(cli, "ingest_reference", acquire)
    assert cli.main(["--data-root", str(tmp_path), command, *flags, "--refresh"]) == 0
    output = capsys.readouterr().out
    assert "local-test-secret" not in output
    assert json.loads(output)["state"] == "complete"
    assert not (tmp_path / "catalog.duckdb").exists()


@pytest.mark.parametrize(
    "command,flags",
    [
        ("fetch-master", ["--date", "2026-10-22"]),
        ("fetch-calendar", ["--from", "2026-10-01", "--to", "2026-10-22"]),
    ],
)
def test_reference_cli_missing_key(monkeypatch, capsys, command, flags):
    monkeypatch.setattr(cli, "api_key", lambda: None)
    assert cli.main([command, *flags]) == 2
    assert "No API call" in capsys.readouterr().out
