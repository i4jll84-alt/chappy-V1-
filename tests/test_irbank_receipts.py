import json
from datetime import date

import pytest

from trading_system.research import irbank_receipts as receipts

DAY = date(2026, 11, 27)


def store(root, run, *, log=True, receipt=True, **change):
    folder = root / DAY.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    if log:
        (folder / (run + ".stdout.log")).write_bytes(b"restricted raw log")
    if receipt:
        payload = {
            "state": "daily_quota_reached",
            "reason": "remote_daily_headroom",
            "started_at": "2026-11-27T21:00:00+09:00",
            "finished_at": "2026-11-27T21:30:00+09:00",
            "before": {"date": "2026-11-27"},
            "after": {"date": "2026-11-27"},
            "batches": [
                {
                    "state": "stopped",
                    "phase": "first5_required",
                    "empty": 2,
                    "errors": 0,
                    "remaining": 3,
                    "requests_this_run": 4,
                }
            ],
            "data_attempts_this_execution": 3,
            "http_attempts_this_execution": 4,
            "comparison": {
                "state": "waiting",
                "branches": {"original": {"state": "waiting"}, "approx": {"state": "complete"}},
            },
            **change,
        }
        (folder / (run + ".json")).write_text(json.dumps(payload), encoding="utf-8")


def test_cli_preserves_every_attempt_and_missing_day_without_reading_stdout(tmp_path, capsys):
    root = tmp_path / "data/irbank_jsf/daily"
    store(root, "210000-000000", state="needs_attention", reason="secret text must stay local")
    store(root, "213000-000000")
    store(root, "223000-000000", receipt=False)
    folder = root / DAY.isoformat()
    (folder / "latest.json").write_bytes((folder / "213000-000000.json").read_bytes())
    (folder / "210000-000000.stdout.log").write_bytes(b"\xff")
    before = {p: p.stat().st_mtime_ns for p in root.rglob("*")}
    result = receipts.main(
        ["--project", str(tmp_path), "--from-date", DAY.isoformat(), "--through", "2026-11-28"]
    )
    output = capsys.readouterr().out
    rows = [json.loads(line) for line in output.splitlines()]
    assert result == 2
    assert [r["status"] for r in rows] == ["error", "finished", "unverified", "unverified"]
    assert [r["reason"] for r in rows] == [
        "see_receipt",
        "remote_daily_headroom",
        "no_finished_receipt",
        "no_timestamped_runs",
    ]
    assert rows[1]["branches"] == {"original": "waiting", "approx": "complete"}
    assert rows[1]["batch_phases"] == ["first5_required"]
    assert rows[1]["local_quota"]["remaining"] is None and rows[1]["provider_quota"] is None
    assert "secret" not in output
    assert {p: p.stat().st_mtime_ns for p in root.rglob("*")} == before


@pytest.mark.parametrize(
    "change",
    [
        {"state": "new_state"},
        {"state": "archive_complete"},
        {"finished_at": "2026-11-27T20:00:00+09:00"},
        {"started_at": "2026-11-27T21:00:00"},
        {"after": {"date": "2026-11-28"}},
        {"comparison": {"state": "complete", "branches": {"original": {"state": "waiting"}}}},
        {"data_attempts_this_execution": False},
        {"http_attempts_this_execution": 99},
        {"last_remote_usage": {"used": False}},
        {"last_remote_usage": {"used": 100, "limit": 100, "remaining": 1}},
        {"last_remote_usage": {"checked_at": "2026-11-27T23:00:00+09:00"}},
        {
            "after": {
                "date": "2026-11-27",
                "daily_data_attempts": 200,
                "daily_data_limit": 1000,
                "remaining_daily_data": 999,
            }
        },
        {"after": {"date": "2026-11-27", "checked_at": "2026-11-26T23:00:00+09:00"}},
    ],
)
def test_inconsistent_receipt_is_unverified(tmp_path, change):
    store(tmp_path, "210000-000000", **change)
    result = receipts.inspect_day(tmp_path, DAY)
    assert result == [
        {
            "run": "210000-000000",
            "status": "unverified",
            "reason": "unreadable_or_inconsistent_receipt",
        }
    ]


def test_malformed_and_json_only_attempts(tmp_path):
    store(tmp_path, "210000-000000", log=False)
    store(tmp_path, "213000-000000")
    (tmp_path / DAY.isoformat() / "213000-000000.json").write_bytes(b"{")
    rows = receipts.inspect_day(tmp_path, DAY)
    assert [r["reason"] for r in rows] == ["missing_stdout", "unreadable_or_inconsistent_receipt"]


def test_midnight_end_is_reported_under_the_start_date(tmp_path):
    store(
        tmp_path,
        "210000-000000",
        state="day_changed",
        reason="day_changed_resume",
        finished_at="2026-11-28T00:00:01+09:00",
        after={"date": "2026-11-28"},
    )
    row = receipts.inspect_day(tmp_path, DAY)[0]
    assert row["status"] == "partial" and row["finished_at"].startswith("2026-11-28")


def test_local_and_provider_balances_keep_source_dates_and_unknown_legacy_fields(tmp_path):
    after = {
        "date": "2026-11-27",
        "checked_at": "2026-11-27T21:30:00+09:00",
        "daily_data_attempts": 200,
        "daily_data_limit": 1000,
        "remaining_daily_data": 800,
        "total_data_limit": 10000,
        "remaining_trial_data": 8500,
    }
    remote = {
        "limit": 200,
        "used": 200,
        "remaining": 0,
        "checked_at": "2026-11-26T22:00:00+09:00",
        "unexpected": "private",
    }
    store(tmp_path, "210000-000000", after=after, last_remote_usage=remote)
    row = receipts.inspect_day(tmp_path, DAY)[0]
    assert row["local_quota"] == {
        "date": "2026-11-27",
        "checked_at": "2026-11-27T21:30:00+09:00",
        "used": 200,
        "limit": 1000,
        "remaining": 800,
        "trial_remaining": 8500,
        "authorized_remaining": None,
    }
    assert row["provider_quota"] == {
        "used": 200,
        "limit": 200,
        "remaining": 0,
        "checked_at": "2026-11-26T22:00:00+09:00",
        "same_local_day": False,
    }
    after.update(
        trial_data_attempts=1500,
        remaining_authorized_data=8500,
        authorized_period_data_attempts=1500,
    )
    store(tmp_path, "210001-000000", after=after, last_remote_usage={"remaining": 0})
    row = receipts.inspect_day(tmp_path, DAY)[1]
    assert row["local_quota"]["authorized_remaining"] == 8500
    assert row["provider_quota"]["used"] is None and row["provider_quota"]["same_local_day"] is None
