import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from trading_system.free_market_startup import check_startup

START = datetime.fromisoformat("2026-10-31T01:00:00+09:00")


def no_fetch(*args, **kwargs):
    pytest.fail("unexpected acquisition")


def runner(report, code=0, calls=None):
    def run(command, **kwargs):
        if calls is not None:
            calls.append(command)
        kwargs["stdout"].write(json.dumps(report) + "\n")
        return SimpleNamespace(returncode=code)

    return run


def test_wait_until_full_30_minutes_without_creating_state(tmp_path):
    result, code = check_startup(
        tmp_path, START, 10, moment=START + timedelta(minutes=30, microseconds=-1), runner=no_fetch
    )
    assert result["state"] == "waiting_for_30_minutes"
    assert code == 0
    assert not (tmp_path / "scheduler").exists()


def test_exact_30_minutes_fetches_latest_friday_once(tmp_path):
    calls = []
    result, code = check_startup(
        tmp_path,
        START,
        10,
        moment=START + timedelta(minutes=30),
        runner=runner({"state": "checked", "issues": [], "added_or_changed": 3}, calls=calls),
    )
    assert result["due_at"] == "2026-10-30T08:00:00+09:00"
    assert result["state"] == "checked" and code == 0
    assert calls[0][-3:] == ["--root", str(tmp_path.resolve()), "--due"]
    assert result["result"]["added_or_changed"] == 3
    result, code = check_startup(
        tmp_path, START, 10, moment=START + timedelta(hours=1), runner=no_fetch
    )
    assert result["state"] == "startup_already_handled" and code == 0


def test_success_after_release_skips_network(tmp_path):
    (tmp_path / "report-example.json").write_text(
        json.dumps(
            {
                "run": "20261030T100000-example",
                "checked_at": "2026-10-30T10:15:00+00:00",
                "state": "checked",
                "issues": [],
            }
        ),
        encoding="utf-8",
    )
    result, code = check_startup(
        tmp_path, START, 10, moment=START + timedelta(minutes=30), runner=no_fetch
    )
    assert result["state"] == "already_acquired" and code == 0


def test_stop_is_preserved(tmp_path):
    (tmp_path / "STOP").touch()
    result, code = check_startup(
        tmp_path, START, 10, moment=START + timedelta(minutes=30), runner=no_fetch
    )
    assert result["state"] == "stopped" and code == 0
    assert (tmp_path / "STOP").exists()


@pytest.mark.parametrize(
    "report,code",
    [
        ({"state": "issues_found", "issues": ["network"]}, 1),
        ({"state": "checked", "issues": ["bad_hash"]}, 0),
        ({"state": "checked", "issues": []}, 3),
        ({}, 0),
    ],
)
def test_failed_attempt_is_retained_without_minute_retries(tmp_path, report, code):
    result, exit_code = check_startup(
        tmp_path, START, 10, moment=START + timedelta(minutes=30), runner=runner(report, code)
    )
    assert result["state"] == "failed" and exit_code != 0
    assert (tmp_path / result["log"]).exists()
    check_startup(tmp_path, START, 10, moment=START + timedelta(hours=1), runner=no_fetch)
    stored = json.loads((tmp_path / "scheduler/startup-status.json").read_text("utf-8"))
    assert stored["state"] == "failed"
    result, exit_code = check_startup(
        tmp_path,
        START + timedelta(hours=1),
        20,
        moment=START + timedelta(hours=2),
        runner=runner({"state": "checked", "issues": []}),
    )
    assert result["state"] == "checked" and exit_code == 0


def test_existing_collector_is_rechecked_without_marking_acquired(tmp_path):
    result, _ = check_startup(
        tmp_path,
        START,
        10,
        moment=START + timedelta(minutes=30),
        runner=runner({"state": "already_running"}),
    )
    assert result["state"] == "already_running"
    calls = []
    result, _ = check_startup(
        tmp_path,
        START,
        10,
        moment=START + timedelta(minutes=31),
        runner=runner({"state": "checked", "issues": []}, calls=calls),
    )
    assert len(calls) == 1 and result["state"] == "checked"


def test_interrupted_checker_does_not_count_as_completed(tmp_path):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        check_startup(tmp_path, START, 10, moment=START + timedelta(minutes=30), runner=interrupt)
    saved = json.loads((tmp_path / "scheduler/startup-status.json").read_text("utf-8"))
    assert saved["state"] == "running"
    result, _ = check_startup(
        tmp_path,
        START,
        10,
        moment=START + timedelta(minutes=31),
        runner=runner({"state": "checked", "issues": []}),
    )
    assert result["state"] == "checked"


def test_naive_and_future_start_are_rejected(tmp_path):
    for started in (START.replace(tzinfo=None), START + timedelta(hours=1)):
        with pytest.raises(ValueError):
            check_startup(tmp_path, started, 10, moment=START, runner=no_fetch)
