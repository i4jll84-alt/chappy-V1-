"""Bounded read-only observations of an already-open dedicated Excel workbook.

Excel accessibility is distinct from RSS connectivity. Stable cell values do not
prove an active feed, and unchanged values do not by themselves prove a disconnect.
"""

import argparse
import json
import math
import os
import re
import subprocess
import time
import uuid
from datetime import UTC, date, datetime
from datetime import time as wall_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_system.data_sources.rss_snapshot import ITEMS
from trading_system.storage.store import atomic_write, canonical, normalized_payload

ERRORS = {
    "invalid_target",
    "excel_not_registered",
    "excel_not_ready",
    "target_not_in_attached_instance",
    "unexpected_sheet_layout",
    "excel_read_failed",
}


def read_once(workbook: Path, timeout: float = 10) -> dict:
    """Use a disposable helper so a blocked COM call has a bounded lifetime."""
    if os.name != "nt":
        return {"status": "error", "reason": "windows_required"}
    script = Path(__file__).resolve().parents[3] / "scripts" / "read_rss_excel.ps1"
    shell = Path(os.environ.get("SystemRoot", "C:/Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        result = subprocess.run(
            [
                str(shell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-WorkbookPath",
                str(workbook),
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "reason": "excel_read_timeout"}
    except (OSError, UnicodeError):
        return {"status": "error", "reason": "helper_failed"}
    if result.returncode != 0:
        return {"status": "error", "reason": "helper_failed"}
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {"status": "error", "reason": "invalid_helper_response"}
    if not isinstance(payload, dict):
        return {"status": "error", "reason": "invalid_helper_response"}
    if payload.get("status") == "error":
        reason = payload.get("reason")
        if reason not in ERRORS:
            return {"status": "error", "reason": "invalid_helper_response"}
    return payload


def validate_read(raw: dict, code: str, observed_at: datetime) -> dict:
    """Validate the two reads without assuming they form a transactional quote."""
    if not re.fullmatch(r"[0-9A-Z]{4,5}\.T", code):
        raise ValueError("invalid_expected_code")
    if observed_at.utcoffset() is None:
        raise ValueError("observation_timezone_required")
    result = {
        "schema_version": 1,
        "source": "rakuten_ms2_rss_excel_cells",
        "code": code,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "status": "attention_required",
        "issues": [],
        "excel_read_verified": False,
        "rss_connection_status": "unknown",
        "freshness_verified": False,
        "source_updated_at": None,
        "published_at": None,
        "quote_date": None,
        "normalized": {"price": None, "quote_time": None, "current_date": None},
    }
    if raw.get("status") == "error":
        allowed = ERRORS | {
            "windows_required",
            "helper_failed",
            "invalid_helper_response",
            "excel_read_timeout",
        }
        reason = raw.get("reason")
        result["issues"] = [reason if reason in allowed else "invalid_helper_response"]
        return result
    keys = ["formulas_before", "formulas_after", "values_before", "values_after"]
    if (
        raw.get("schema_version") != 1
        or raw.get("status") != "read"
        or any(not isinstance(raw.get(key), list) or len(raw[key]) != 3 for key in keys)
    ):
        result["issues"] = ["invalid_helper_response"]
        return result
    for position, item in enumerate(ITEMS.values()):
        expected = f'=RssMarket("{code}","{item}")'
        allowed = {expected, expected.replace("=RssMarket", "=_xll.RssMarket")}
        if any(
            not isinstance(raw[key][position], str) or raw[key][position] not in allowed
            for key in keys[:2]
        ):
            result["issues"] = ["unexpected_formula"]
            return result
    result["excel_read_verified"] = True
    if raw["values_before"] != raw["values_after"]:
        result["issues"] = ["values_changed_during_read"]
        return result
    values = raw["values_after"]
    price, clock, day = values
    try:
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise InvalidOperation
        number = Decimal(str(price))
        if not number.is_finite() or number <= 0:
            raise InvalidOperation
        result["normalized"]["price"] = str(number)
    except InvalidOperation:
        result["issues"].append("invalid_price")
    try:
        if not isinstance(clock, str) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", clock):
            raise ValueError
        result["normalized"]["quote_time"] = wall_time.fromisoformat(clock).isoformat()
    except ValueError:
        result["issues"].append("invalid_quote_time")
    try:
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", day):
            raise ValueError
        current_day = date.fromisoformat(day.replace("/", "-"))
        result["normalized"]["current_date"] = current_day.isoformat()
        if current_day != observed_at.astimezone(ZoneInfo("Asia/Tokyo")).date():
            result["issues"].append("current_date_not_today")
    except ValueError:
        result["issues"].append("invalid_current_date")
    if not result["issues"]:
        result["status"] = "observed"
    return result


def observe(
    workbook: Path,
    code: str,
    output_root: Path,
    *,
    samples: int = 3,
    interval: float = 2,
    timeout: float = 10,
    unchanged_after: float = 60,
    expect_updates: bool = False,
    reader=read_once,
    clock=time.monotonic,
    sleeper=time.sleep,
    utcnow=lambda: datetime.now(UTC),
    stop_requested=None,
) -> tuple[Path | None, dict]:
    """Record each bounded attempt. Never take order or application recovery actions."""
    if not re.fullmatch(r"[0-9A-Z]{4,5}\.T", code):
        raise ValueError("invalid_expected_code")
    if (
        not isinstance(samples, int)
        or isinstance(samples, bool)
        or not 1 <= samples <= 60
        or not all(math.isfinite(n) for n in (interval, timeout, unchanged_after))
        or not 1 <= interval <= 60
        or not 1 <= timeout <= 15
        or unchanged_after <= 0
        or samples * (interval + timeout) > 300
    ):
        raise ValueError("invalid_sampling_limits")
    run = None
    signature = None
    changed_at = None
    last_clock = None
    attention = 0
    read_count = 0
    changed_count = 0
    captured = 0
    for index in range(samples):
        if stop_requested and stop_requested():
            break
        if index:
            sleeper(interval)
        if stop_requested and stop_requested():
            break
        if run is None:
            run = output_root / uuid.uuid4().hex
            run.mkdir(parents=True, exist_ok=False)
        started_at = utcnow()
        raw = reader(workbook, timeout)
        completed_at = utcnow()
        tick = clock()
        result = validate_read(raw, code, completed_at)
        result["read_started_at"] = started_at.astimezone(UTC).isoformat()
        result["read_completed_at"] = completed_at.astimezone(UTC).isoformat()
        result["raw"] = normalized_payload(raw)
        result["expect_updates"] = expect_updates
        result["unchanged_seconds"] = None
        read_count += int(result["excel_read_verified"])
        if not math.isfinite(tick) or (last_clock is not None and tick < last_clock):
            raise ValueError("invalid_monotonic_clock")
        last_clock = tick
        if result["status"] == "observed":
            current = canonical(result["normalized"])
            if current != signature:
                changed_count += int(signature is not None)
                signature, changed_at = current, tick
            result["unchanged_seconds"] = tick - changed_at
            if expect_updates and tick - changed_at >= unchanged_after:
                result["issues"].append("unchanged_values")
                result["status"] = "attention_required"
        else:
            # A gap cannot establish continuous non-updating time.
            signature, changed_at = None, None
        attention += int(result["status"] != "observed")
        atomic_write(run / f"{index:03d}.json", canonical(result).encode("utf-8"))
        captured += 1
    summary = {
        "schema_version": 1,
        "status": "attention_required" if attention else "observed",
        "samples": captured,
        "excel_reads": read_count,
        "attention_samples": attention,
        "changed_observations": changed_count,
        "expect_updates": expect_updates,
        "rss_connection_status": "unknown",
        "freshness_verified": False,
    }
    if captured < samples:
        summary["capture_stopped_early"] = True
    if run is not None:
        atomic_write(run / "summary.json", canonical(summary).encode("utf-8"))
    return run, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/rss-live"))
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--expect-updates", action="store_true")
    parser.add_argument("--unchanged-after", type=float, default=60)
    args = parser.parse_args(argv)
    try:
        run, summary = observe(
            args.workbook,
            args.code,
            args.output_root,
            samples=args.samples,
            interval=args.interval,
            timeout=args.timeout,
            expect_updates=args.expect_updates,
            unchanged_after=args.unchanged_after,
        )
    except (OSError, ValueError):
        print(json.dumps({"status": "failed", "reason": "observation_failed"}))
        return 2
    print(json.dumps({**summary, "report_dir": str(run)}, ensure_ascii=False))
    return 0 if summary["status"] == "observed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
