import copy
import hashlib
import json
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading_system.adjusted_history import (
    Adjuster,
    build_adjusted,
    pinned_inputs,
    positive_decimal,
    rounded_tenth,
)
from trading_system.history_download import Archive, identity
from trading_system.storage.store import canonical


def row(day, price="100", factor="1", event="", code="01230"):
    return {
        "Date": day,
        "Code": code,
        "O": price,
        "H": price,
        "L": price,
        "C": price,
        "Vo": "1000",
        "Va": "100000",
        "AdjFactor": factor,
        "ExRT": event,
    }


def test_split_coefficient_applies_to_older_rows_only():
    a = Adjuster(date(2026, 10, 30))
    today = a.transform(row("2026-10-30", "50", "0.5", "1"))
    yesterday = a.transform(row("2026-10-29", "100"))
    assert today["AdjC"] == "50.0" and today["AdjVo"] == "1000.0"
    assert yesterday["AdjC"] == "50.0" and yesterday["AdjVo"] == "2000.0"


def test_rights_issue_changes_prices_but_not_volume_and_composes_with_consolidation():
    a = Adjuster(date(2026, 10, 30))
    a.transform(row("2026-10-30", "100", "0.8", "3"))
    rights = a.transform(row("2026-10-29", "100", "10", "2"))
    older = a.transform(row("2026-10-28", "10"))
    assert rights["AdjC"] == "80.0" and rights["AdjVo"] == "1000.0"
    assert older["AdjC"] == "80.0" and older["AdjVo"] == "100.0"
    assert a.events == {"1": 0, "2": 1, "3": 1}


def test_no_trade_day_preserves_nulls_and_event():
    a = Adjuster(date(2026, 10, 30))
    no_trade = row("2026-10-30", "", "0.5", "1")
    no_trade.update(Vo="", Va="")
    original = copy.deepcopy(no_trade)
    output = a.transform(no_trade)
    assert all(output[key] is None for key in ("AdjO", "AdjH", "AdjL", "AdjC", "AdjVo"))
    assert a.transform(row("2026-10-29"))["AdjC"] == "50.0"
    assert no_trade == original


def test_anchor_excludes_later_events_and_other_codes_reset_coefficients():
    a = Adjuster(date(2026, 10, 29))
    assert a.transform(row("2026-10-30", "1", "0.01", "1")) is None
    assert a.transform(row("2026-10-29", "100", "0.5", "1"))["AdjC"] == "100.0"
    assert a.transform(row("2026-10-29", "100", code="130A0"))["AdjC"] == "100.0"


def test_exact_final_half_up_rounding_without_intermediate_rounding():
    assert rounded_tenth(Decimal("1.25"), Fraction(1)) == "1.3"
    assert rounded_tenth(Decimal("1.15"), Fraction(1)) == "1.2"
    a = Adjuster(date(2026, 10, 30))
    a.transform(row("2026-10-30", factor="0.333", event="1"))
    a.transform(row("2026-10-29", factor="0.333", event="1"))
    assert a.transform(row("2026-10-28", "100"))["AdjC"] == "11.1"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1", "", None, 1.25])
def test_invalid_inputs_are_rejected(value):
    with pytest.raises(ValueError):
        positive_decimal(value)


@pytest.mark.parametrize(("factor", "event"), [("0.5", ""), ("1", "9")])
def test_unknown_event_is_not_assumed_a_split(factor, event):
    with pytest.raises(ValueError, match="unknown_adjustment_event"):
        Adjuster(date(2026, 10, 30)).transform(row("2026-10-30", factor=factor, event=event))


def test_duplicates_and_partial_missing_values_are_rejected():
    a = Adjuster(date(2026, 10, 30))
    a.transform(row("2026-10-30"))
    with pytest.raises(ValueError, match="duplicate_or_date_order"):
        a.transform(row("2026-10-30"))
    bad = row("2026-10-30")
    bad["C"] = ""
    with pytest.raises(ValueError, match="partial_market_values"):
        Adjuster(date(2026, 10, 30)).transform(bad)


def make_archive(root):
    archive = Archive(root)
    inputs = [row("2026-10-29"), row("2026-10-30", "50", "0.5", "1")]
    path = root / "normalized" / "sample.parquet"
    path.parent.mkdir()
    pq.write_table(pa.Table.from_pylist(inputs), path)
    raw, raw_hash = archive.immutable(b"synthetic retained source")
    fields = {
        "id": "sample",
        "endpoint": "/equities/bars/daily",
        "object_key": "equities/bars/daily/sample.csv.gz",
        "modified": "2026-10-30T09:00:00+00:00",
        "state": "complete",
        "raw_path": raw,
        "sha256": raw_hash,
        "parquet_path": "normalized/sample.parquet",
        "parquet_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": 2,
        "available_at": "2026-10-30T10:00:01+00:00",
    }
    with archive.db:
        archive.db.execute(
            "INSERT INTO bulk_files("
            + ",".join(fields)
            + ") VALUES("
            + ",".join("?" for _ in fields)
            + ")",
            list(fields.values()),
        )
    archive.close()
    quality = root / "quality.json"
    quality.write_text(
        canonical(
            {
                "state": "checked",
                "issues": [],
                "start": "2026-10-29",
                "end": "2026-10-30",
                "input_manifest": [fields],
                "input_manifest_sha256": identity([fields]),
            }
        ),
        encoding="utf-8",
    )
    return quality, path


def test_build_is_separate_reproducible_and_preserves_lineage(tmp_path):
    root = tmp_path / "archive"
    quality, original = make_archive(root)
    before = original.read_bytes()
    reports = []
    for name in ("first", "second"):
        destination = tmp_path / name
        result = build_adjusted(root, quality, destination, date(2026, 10, 30), batch_size=1)
        reports.append(result)
        table = pq.read_table(destination / "adjusted.parquet")
        assert table.schema.metadata[b"historical_point_in_time_verified"] == b"false"
        rows = table.to_pylist()
        assert [(r["Date"], r["AdjC"]) for r in rows] == [
            ("2026-10-30", "50.0"),
            ("2026-10-29", "50.0"),
        ]
        assert all(r["source_file_id"] == "sample" for r in rows)
        assert result["historical_backtest_ready"] is False
        assert result["available_at"] > result["completed_at"]
    assert reports[0]["sha256"] == reports[1]["sha256"]
    assert original.read_bytes() == before


def test_new_revision_and_changed_bytes_block_derivation(tmp_path):
    root = tmp_path / "archive"
    quality, original = make_archive(root)
    original.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="input_file_changed"):
        pinned_inputs(root, quality, date(2026, 10, 30))
    archive = Archive(root)
    with archive.db:
        archive.db.execute("UPDATE bulk_files SET state='pending'")
    archive.close()
    with pytest.raises(ValueError, match="quality_snapshot_changed"):
        pinned_inputs(root, quality, date(2026, 10, 30))


def test_existing_output_and_stale_quality_are_rejected(tmp_path):
    root = tmp_path / "archive"
    quality, _ = make_archive(root)
    destination = tmp_path / "output"
    destination.mkdir()
    with pytest.raises(ValueError, match="new_derived_directory_required"):
        build_adjusted(root, quality, destination, date(2026, 10, 30))
    report = json.loads(quality.read_text(encoding="utf-8"))
    report["state"] = "issues_found"
    quality.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="quality_evidence_unusable"):
        pinned_inputs(root, quality, date(2026, 10, 30))


def test_failed_build_never_publishes_complete_dataset(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    quality, original = make_archive(root)
    before = original.read_bytes()
    original_transform = Adjuster.transform

    def interrupted(self, value):
        if self.rows:
            raise ValueError("simulated_interruption")
        return original_transform(self, value)

    monkeypatch.setattr(Adjuster, "transform", interrupted)
    destination = tmp_path / "failed"
    with pytest.raises(ValueError, match="simulated_interruption"):
        build_adjusted(root, quality, destination, date(2026, 10, 30), batch_size=1)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "failed"
    assert not (destination / "adjusted.parquet").exists()
    assert (destination / "adjusted.parquet.part").exists()
    assert original.read_bytes() == before


def test_derived_availability_cannot_precede_inputs(tmp_path):
    from datetime import datetime

    root = tmp_path / "archive"
    quality, _ = make_archive(root)
    future = "2030-01-01T00:00:00+00:00"
    archive = Archive(root)
    with archive.db:
        archive.db.execute("UPDATE bulk_files SET available_at=?", (future,))
    archive.close()
    report = json.loads(quality.read_text(encoding="utf-8"))
    report["input_manifest"][0]["available_at"] = future
    report["input_manifest_sha256"] = identity(report["input_manifest"])
    quality.write_text(canonical(report), encoding="utf-8")
    result = build_adjusted(root, quality, tmp_path / "future", date(2026, 10, 30))
    assert datetime.fromisoformat(result["available_at"]) > datetime.fromisoformat(future)
    assert result["historical_backtest_ready"] is False


@pytest.mark.parametrize(
    ("coefficient", "expected"),
    [
        ("0.9090909090909092", "15373.0"),
        ("0.9090909090909091", "15373.1"),
    ],
)
def test_coefficient_precision_difference_remains_visible(coefficient, expected):
    a = Adjuster(date(2026, 10, 30))
    a.transform(row("2026-03-30", factor="0.9523809523809523", event="1"))
    for stamp in ("2024-03-28", "2023-03-30", "2022-03-30", "2021-03-30"):
        a.transform(row(stamp, factor=coefficient, event="1"))
    source = row("2020-05-12")
    source["Vo"] = "10000"
    source["Va"] = "1000000"
    assert a.transform(source)["AdjVo"] == expected
