"""Build a separately labelled, retrospective adjusted-price dataset.

Exact rational arithmetic implements the provider's final half-up rounding.
A historical adjustment anchor is not evidence of historical availability.
"""

import argparse
import json
import os
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from trading_system.history_audit import safe_path
from trading_system.history_download import identity, now, sha_file
from trading_system.storage.store import atomic_write, canonical

RAW = ("O", "H", "L", "C", "Vo", "Va")
ADJUSTED = ("AdjO", "AdjH", "AdjL", "AdjC", "AdjVo")
COLUMNS = (
    "Date",
    "Code",
    *ADJUSTED,
    "PriceFactorNumerator",
    "PriceFactorDenominator",
    "VolumeFactorNumerator",
    "VolumeFactorDenominator",
    "source_file_id",
)
DAILY = "/equities/bars/daily"


def positive_decimal(value):
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ValueError("invalid_numeric_string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid_numeric_string") from None
    if not number.is_finite() or number <= 0 or abs(number.as_tuple().exponent) > 100:
        raise ValueError("nonpositive_or_nonfinite_number")
    return number


def rounded_tenth(value, multiplier):
    """Round a positive exact rational to tenths without a floating-point step."""
    numerator, denominator = value.as_integer_ratio()
    numerator *= multiplier.numerator
    denominator *= multiplier.denominator
    whole, remainder = divmod(numerator * 10, denominator)
    if remainder * 2 >= denominator:
        whole += 1
    return f"{whole // 10}.{whole % 10}"


class Adjuster:
    def __init__(self, anchor):
        self.anchor = anchor.isoformat()
        self.code = self.previous_date = None
        self.price = self.volume = Fraction(1)
        self.events = {"1": 0, "2": 0, "3": 0}
        self.no_trade_rows = self.rows = self.codes = 0

    def transform(self, row):
        stamp, code = row["Date"], row["Code"]
        if not isinstance(stamp, str) or date.fromisoformat(stamp).isoformat() != stamp:
            raise ValueError("invalid_date")
        if not isinstance(code, str) or not re.fullmatch("[0-9][0-9A-Z]{4}", code):
            raise ValueError("invalid_code")
        if stamp > self.anchor:
            return None
        if self.code is not None and code < self.code:
            raise ValueError("code_order")
        if code != self.code:
            self.code, self.previous_date = code, None
            self.price = self.volume = Fraction(1)
            self.codes += 1
        if self.previous_date is not None and stamp >= self.previous_date:
            raise ValueError("duplicate_or_date_order")
        self.previous_date = stamp
        factor = Fraction(positive_decimal(row["AdjFactor"]))
        event = row["ExRT"] or ""
        if event not in ("", "1", "2", "3") or (factor != 1 and not event):
            raise ValueError("unknown_adjustment_event")
        values = [row[f] for f in RAW]
        missing = [v is None or v == "" for v in values]
        if any(missing) and not all(missing):
            raise ValueError("partial_market_values")
        numbers = [] if all(missing) else [positive_decimal(v) for v in values]
        if numbers and not (
            numbers[2] <= numbers[0] <= numbers[1] and numbers[2] <= numbers[3] <= numbers[1]
        ):
            raise ValueError("ohlc_order")
        result = {
            "Date": stamp,
            "Code": code,
            "PriceFactorNumerator": str(self.price.numerator),
            "PriceFactorDenominator": str(self.price.denominator),
            "VolumeFactorNumerator": str(self.volume.numerator),
            "VolumeFactorDenominator": str(self.volume.denominator),
        }
        for i, field in enumerate(ADJUSTED):
            multiplier = self.price if i < 4 else 1 / self.volume
            result[field] = rounded_tenth(numbers[i], multiplier) if numbers else None
        if not numbers:
            self.no_trade_rows += 1
        # The current day's coefficient applies only to older observations.
        self.price *= factor
        if event != "3":
            self.volume *= factor
        if event:
            self.events[event] += 1
        self.rows += 1
        return result


def pinned_inputs(root, quality_path, anchor):
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    if (
        quality.get("state") != "checked"
        or quality.get("issues")
        or quality.get("input_manifest_sha256") != identity(quality.get("input_manifest"))
        or quality.get("start", "9999") > anchor.isoformat()
        or quality.get("end", "") < anchor.isoformat()
    ):
        raise ValueError("quality_evidence_unusable")
    with sqlite3.connect((root / "history.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        current = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM (SELECT *,row_number() OVER(PARTITION BY endpoint,object_key "
                "ORDER BY julianday(modified) DESC,rowid DESC) rank FROM bulk_files "
                "WHERE endpoint=?) WHERE rank=1 ORDER BY object_key",
                (DAILY,),
            )
        ]
    expected = [r for r in quality["input_manifest"] if r["endpoint"] == DAILY]
    by_id = {r["id"]: r for r in expected}
    if not current or {r["id"] for r in current} != set(by_id):
        raise ValueError("quality_snapshot_changed")
    for row in current:
        if row["state"] != "complete" or any(row[k] != v for k, v in by_id[row["id"]].items()):
            raise ValueError("quality_snapshot_changed")
        for path_key, hash_key in (("raw_path", "sha256"), ("parquet_path", "parquet_sha256")):
            path = safe_path(root, row[path_key])
            if not path.is_file() or sha_file(path) != row[hash_key]:
                raise ValueError("input_file_changed")
    for row in current:
        stamp = datetime.fromisoformat(row["available_at"])
        if stamp.tzinfo is None:
            raise ValueError("naive_input_availability")
    return current, quality["input_manifest_sha256"]


def build_adjusted(root, quality_path, destination, anchor, batch_size=20000):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if (
        destination.exists()
        or destination.is_relative_to(root / "raw")
        or destination.is_relative_to(root / "normalized")
    ):
        raise ValueError("new_derived_directory_required")
    if batch_size < 1:
        raise ValueError("invalid_batch_size")
    quality_path = Path(quality_path).resolve()
    inputs, quality_hash = pinned_inputs(root, quality_path, anchor)
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {
        "state": "building",
        "created_at": now(),
        "anchor_date": anchor.isoformat(),
        "dataset_kind": "retrospective_adjusted_prices",
        "historical_point_in_time_verified": False,
        "historical_backtest_ready": False,
        "real_execution_enabled": False,
        "source_published_at": None,
        "source_available_at": None,
        "available_at": None,
        "rounding": "exact_rational_final_half_up_to_0.1",
        "algorithm_source_sha256": sha_file(Path(__file__)),
        "input_available_at_max": max(datetime.fromisoformat(r["available_at"]) for r in inputs)
        .astimezone(UTC)
        .isoformat(),
        "quality_report_sha256": sha_file(quality_path),
        "quality_input_manifest_sha256": quality_hash,
        "inputs": [
            {
                k: r[k]
                for k in (
                    "id",
                    "object_key",
                    "raw_path",
                    "sha256",
                    "parquet_path",
                    "parquet_sha256",
                    "rows",
                    "available_at",
                )
            }
            for r in inputs
        ],
    }
    manifest_path = destination / "manifest.json"
    atomic_write(manifest_path, canonical(manifest).encode("utf-8"))
    working = destination / "adjusted.parquet.part"
    target = destination / "adjusted.parquet"
    try:
        paths = [str(safe_path(root, r["parquet_path"])) for r in inputs]
        ids = {str(safe_path(root, r["parquet_path"])).replace("\\", "/"): r["id"] for r in inputs}
        schema = pa.schema(
            [(name, pa.string()) for name in COLUMNS],
            metadata={
                b"dataset_kind": b"retrospective_adjusted_prices",
                b"historical_point_in_time_verified": b"false",
                b"anchor_date": anchor.isoformat().encode(),
                b"quality_input_manifest_sha256": quality_hash.encode(),
            },
        )
        adjuster = Adjuster(anchor)
        with duckdb.connect(
            config={
                "threads": 4,
                "memory_limit": "1GB",
                "temp_directory": str(destination / "sort-work"),
            }
        ) as db:
            db.read_parquet(paths, filename=True).create_view("source_prices")
            low, high = db.execute("SELECT min(Date),max(Date) FROM source_prices").fetchone()
            if not low <= anchor.isoformat() <= high:
                raise ValueError("anchor_outside_received_data")
            expected_rows = db.execute(
                "SELECT count(*) FROM source_prices WHERE Date<=?", [anchor.isoformat()]
            ).fetchone()[0]
            reader = db.execute(
                "SELECT * FROM source_prices WHERE Date<=? ORDER BY Code,Date DESC",
                [anchor.isoformat()],
            ).to_arrow_reader(batch_size)
            with pq.ParquetWriter(working, schema, compression="zstd") as writer:
                for batch in reader:
                    output = []
                    for row in batch.to_pylist():
                        adjusted = adjuster.transform(row)
                        adjusted["source_file_id"] = ids[row["filename"].replace("\\", "/")]
                        output.append(adjusted)
                    writer.write_table(pa.Table.from_pylist(output, schema=schema))
                    if adjuster.rows % 500000 == 0:
                        print(canonical({"phase": "adjusting", "rows": adjuster.rows}), flush=True)
        if pq.read_metadata(working).num_rows != expected_rows or adjuster.rows != expected_rows:
            raise ValueError("derived_row_count_mismatch")
        # Recheck input bytes after processing, not only before reading.
        for row in inputs:
            if sha_file(safe_path(root, row["parquet_path"])) != row["parquet_sha256"]:
                raise ValueError("input_changed_during_build")
        os.replace(working, target)
        manifest.update(
            state="complete",
            completed_at=now(),
            available_at=(
                max(datetime.now(UTC), datetime.fromisoformat(manifest["input_available_at_max"]))
                + timedelta(seconds=1)
            ).isoformat(),
            file="adjusted.parquet",
            sha256=sha_file(target),
            rows=adjuster.rows,
            codes=adjuster.codes,
            no_trade_rows=adjuster.no_trade_rows,
            event_rows=adjuster.events,
            first_date=low,
        )
    except Exception as exc:
        manifest.update(state="failed", error_type=type(exc).__name__, completed_at=now())
        atomic_write(manifest_path, canonical(manifest).encode("utf-8"))
        raise
    atomic_write(manifest_path, canonical(manifest).encode("utf-8"))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/jquants/history"))
    parser.add_argument("--quality-report", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    destination = args.output or Path("data/jquants/adjusted") / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    )
    result = build_adjusted(args.root, args.quality_report, destination, args.as_of)
    print(json.dumps({k: v for k, v in result.items() if k != "inputs"}, ensure_ascii=False))
    print(str(destination.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
