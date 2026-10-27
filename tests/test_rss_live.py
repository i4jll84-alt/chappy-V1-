import copy
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_system.data_sources import rss_live
from trading_system.data_sources.rss_snapshot import ITEMS

NOW = datetime(2026, 1, 5, 8, tzinfo=UTC)


def reading(price=123):
    formulas = [f'=RssMarket("1234.T","{item}")' for item in ITEMS.values()]
    return {
        "schema_version": 1,
        "status": "read",
        "formulas_before": formulas.copy(),
        "formulas_after": formulas.copy(),
        "values_before": [price, "15:30:00", "2026/01/05"],
        "values_after": [price, "15:30:00", "2026/01/05"],
    }


def run(tmp_path, responses, ticks, **kwargs):
    source = iter(responses)
    clock = iter(ticks)
    sleeps = []
    path, summary = rss_live.observe(
        tmp_path / "unused.xlsx",
        "1234.T",
        tmp_path / "runs",
        samples=len(responses),
        reader=lambda *_: next(source),
        clock=lambda: next(clock),
        sleeper=sleeps.append,
        utcnow=lambda: NOW,
        **kwargs,
    )
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(path.glob("0*.json"))]
    return summary, records, sleeps


def test_success_does_not_claim_feed_connectivity_or_freshness():
    result = rss_live.validate_read(reading(), "1234.T", NOW)
    assert result["status"] == "observed"
    assert result["excel_read_verified"] is True
    assert result["rss_connection_status"] == "unknown"
    assert result["freshness_verified"] is False
    assert result["quote_date"] is result["source_updated_at"] is None


def test_changing_values_are_retained_but_not_used(tmp_path):
    raw = reading()
    raw["values_after"][0] = 124
    summary, records, _ = run(tmp_path, [raw], [0])
    assert summary["attention_samples"] == 1
    assert records[0]["issues"] == ["values_changed_during_read"]
    assert records[0]["raw"] == raw
    assert records[0]["normalized"]["price"] is None


@pytest.mark.parametrize("price", [True, None, "123", -2146826246, 0, float("nan"), float("inf")])
def test_invalid_price_or_com_error_code(price):
    result = rss_live.validate_read(reading(price), "1234.T", NOW)
    assert result["status"] == "attention_required"
    assert result["normalized"]["price"] is None


@pytest.mark.parametrize(
    "position,value,issue",
    [
        (1, "25:00:00", "invalid_quote_time"),
        (1, 0.5, "invalid_quote_time"),
        (2, "2026/02/30", "invalid_current_date"),
        (2, "2026/01/04", "current_date_not_today"),
        (2, "2026/01/06", "current_date_not_today"),
    ],
)
def test_invalid_or_other_day(position, value, issue):
    raw = reading()
    for key in ("values_before", "values_after"):
        raw[key][position] = value
    assert issue in rss_live.validate_read(raw, "1234.T", NOW)["issues"]


@pytest.mark.parametrize("formula", ["=OtherFunction()", '=RssMarket("9999.T","現在値")', []])
def test_wrong_formula_rejected(formula):
    raw = reading()
    raw["formulas_before"][0] = formula
    assert rss_live.validate_read(raw, "1234.T", NOW)["issues"] == ["unexpected_formula"]


def test_unchanged_values_default_not_an_alert(tmp_path):
    summary, records, sleeps = run(tmp_path, [reading()] * 3, [0, 60, 120])
    assert summary["status"] == "observed"
    assert records[-1]["unchanged_seconds"] == 120
    assert sleeps == [2, 2]


def test_expected_updates_uses_threshold_without_claiming_disconnect(tmp_path):
    summary, records, _ = run(tmp_path, [reading()] * 3, [0, 59, 60], expect_updates=True)
    assert summary["attention_samples"] == 1
    assert records[-1]["issues"] == ["unchanged_values"]
    assert records[-1]["rss_connection_status"] == "unknown"


def test_failure_resets_unchanged_window_and_keeps_failure(tmp_path):
    responses = [reading(), {"status": "error", "reason": "excel_read_timeout"}, reading()]
    summary, records, _ = run(tmp_path, responses, [0, 60, 120], expect_updates=True)
    assert summary["attention_samples"] == 1
    assert records[1]["issues"] == ["excel_read_timeout"]
    assert records[-1]["unchanged_seconds"] == 0


def test_value_change_resets_window(tmp_path):
    summary, records, _ = run(
        tmp_path, [reading(), reading(124), reading(124)], [0, 59, 60], expect_updates=True
    )
    assert summary["changed_observations"] == 1
    assert summary["attention_samples"] == 0
    assert records[-1]["unchanged_seconds"] == 1


@pytest.mark.parametrize(
    "options",
    [
        {"samples": 0},
        {"samples": 10000},
        {"interval": 0},
        {"timeout": 100},
        {"interval": float("nan")},
        {"samples": 60, "interval": 60},
    ],
)
def test_sampling_limits_reject_before_output(tmp_path, options):
    with pytest.raises(ValueError, match="invalid_sampling_limits"):
        rss_live.observe(tmp_path / "unused", "1234.T", tmp_path / "out", **options)
    assert not (tmp_path / "out").exists()


def test_backward_clock_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid_monotonic_clock"):
        run(tmp_path, [reading()] * 2, [2, 1])


@pytest.mark.skipif(rss_live.os.name != "nt", reason="Windows helper transport")
def test_helper_timeout_and_argument_transport(monkeypatch, tmp_path):
    def timeout(command, **kwargs):
        assert command[-1] == str(tmp_path / "space name.xlsx")
        assert kwargs["timeout"] == 3
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert "-NonInteractive" in command
        raise subprocess.TimeoutExpired(command, 3, stderr="private exception")

    monkeypatch.setattr(rss_live.subprocess, "run", timeout)
    assert rss_live.read_once(tmp_path / "space name.xlsx", 3) == {
        "status": "error",
        "reason": "excel_read_timeout",
    }


@pytest.mark.skipif(rss_live.os.name != "nt", reason="Windows helper transport")
@pytest.mark.parametrize(
    "stdout,returncode,reason",
    [
        ("private error", 1, "helper_failed"),
        ("not json", 0, "invalid_helper_response"),
        ("[]", 0, "invalid_helper_response"),
        ('{"status":"error","reason":"private exception"}', 0, "invalid_helper_response"),
    ],
)
def test_helper_errors_are_sanitized(monkeypatch, stdout, returncode, reason):
    monkeypatch.setattr(
        rss_live.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            stdout=stdout, stderr="private path", returncode=returncode
        ),
    )
    assert rss_live.read_once(Path("unused.xlsx")) == {"status": "error", "reason": reason}


def test_does_not_mutate_input():
    raw = reading()
    expected = copy.deepcopy(raw)
    rss_live.validate_read(raw, "1234.T", NOW)
    assert raw == expected
