import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from trading_system import cli
from trading_system.point_in_time.policy import available_at
from trading_system.settings import Settings


def test_doctor_does_not_expose_key(monkeypatch, capsys):
    from pydantic import SecretStr

    monkeypatch.setattr(cli, "api_key", lambda: SecretStr("DO-NOT-PRINT-THIS"))
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "DO-NOT-PRINT-THIS" not in output
    assert json.loads(output)["api_key_present"] is True


def test_missing_key_does_not_create_data(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "api_key", lambda: None)
    root = tmp_path / "no-data"
    assert (
        cli.main(
            [
                "--data-root",
                str(root),
                "fetch-prices",
                "--code",
                "86970",
                "--from",
                "2026-10-01",
                "--to",
                "2026-10-22",
            ]
        )
        == 2
    )
    assert not root.exists()
    assert "No API call" in capsys.readouterr().out


@pytest.mark.parametrize("setting", [{"stage": 2}, {"live_trading": True}, {"plan": "invented"}])
def test_settings_reject_unsafe_or_unknown_modes(setting):
    with pytest.raises(ValidationError):
        Settings(**setting)


def test_pit_timezone_and_delay():
    jst = timezone(timedelta(hours=9))
    observed = datetime(2026, 10, 23, 17, tzinfo=jst)
    assert available_at(observed) == datetime(2026, 10, 23, 8, 0, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        available_at(datetime(2026, 10, 23))
