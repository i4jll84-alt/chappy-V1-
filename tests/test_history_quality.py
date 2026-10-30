import copy
import gzip
import json
from datetime import date

import duckdb
import pyarrow as pa
import pytest

from trading_system.history_download import Archive
from trading_system.history_quality import audit_quality, inspect_tables, main


def fixture_data():
    prices = [
        {
            "Date": "2026-10-29",
            "Code": "01230",
            "O": "10",
            "H": "12",
            "L": "9",
            "C": "11",
            "Vo": "100",
            "Va": "1100",
            "AdjFactor": "1",
        },
        {
            "Date": "2026-10-29",
            "Code": "130A0",
            "O": "20",
            "H": "22",
            "L": "19",
            "C": "21",
            "Vo": "200",
            "Va": "4200",
            "AdjFactor": "1",
        },
        {
            "Date": "2026-10-30",
            "Code": "130A0",
            "O": "",
            "H": "",
            "L": "",
            "C": "",
            "Vo": "",
            "Va": "",
            "AdjFactor": "0.5",
        },
    ]
    return {
        "prices": prices,
        "master": [{"Date": p["Date"], "Code": p["Code"], "ProdCat": "011"} for p in prices]
        + [{"Date": "2026-11-02", "Code": "130A0", "ProdCat": "011"}],
        "calendar": [{"Date": "2026-10-29", "HolDiv": "1"}, {"Date": "2026-10-30", "HolDiv": "1"}],
        "topix": [{"Date": "2026-10-29"}, {"Date": "2026-10-30"}],
    }


def examine(data, start=None, end=None):
    with duckdb.connect() as db:
        for name, values in data.items():
            db.register(name, pa.Table.from_pylist(values))
        return inspect_tables(db, start, end)


def test_complete_received_snapshot_does_not_assume_universe_or_delisting():
    report = examine(fixture_data())
    assert not any(report["checks"].values())
    assert report["yearly"][0]["no_trade_rows"] == 1
    assert report["last_seen_before_end"] == 1
    assert report["adjustment_rows"] == 1
    assert "優先株" in report["products"][0]["label"]


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        ("O", "not-a-number", "invalid_numeric_O"),
        ("H", "Infinity", "invalid_numeric_H"),
        ("L", "NaN", "invalid_numeric_L"),
        ("C", "50", "ohlc_outside_range"),
        ("Vo", "0", "nonpositive_Vo"),
        ("Va", "", "partial_empty_market_fields"),
        ("AdjFactor", "0", "invalid_adjustment_factor"),
        ("Date", "2026-02-30", "invalid_date"),
        ("Code", "123", "invalid_code"),
    ],
)
def test_invalid_values_are_reported_with_original_evidence(field, value, rule):
    data = fixture_data()
    data["prices"][0][field] = value
    original = copy.deepcopy(data)
    report = examine(data)
    assert report["checks"][rule] > 0
    assert report["samples"][rule][0][field] == value
    assert data == original


def test_cross_dataset_mismatches_and_duplicates_are_detected():
    data = fixture_data()
    data["master"][0]["Code"] = "99990"
    data["master"][1]["ProdCat"] = ""
    data["calendar"][1]["HolDiv"] = "3"
    data["topix"] = [data["topix"][0], data["topix"][0]]
    report = examine(data)
    for rule in (
        "price_without_same_day_master",
        "master_without_same_day_price",
        "unknown_product_category",
        "price_date_not_equity_business_day",
        "price_date_without_topix",
        "duplicate_topix_dates",
    ):
        assert report["checks"][rule] > 0


def test_expected_end_prevents_a_missing_last_day_being_hidden():
    data = fixture_data()
    data["prices"] = data["prices"][:2]
    report = examine(data, date(2026, 10, 29), date(2026, 10, 30))
    assert report["checks"]["business_day_without_prices"] == 1
    assert report["checks"]["master_without_same_day_price"] == 1
    assert report["checks"]["topix_date_without_prices"] == 1


def test_missing_calendar_day_and_duplicate_prices_are_detected():
    data = fixture_data()
    data["calendar"] = data["calendar"][:1]
    data["prices"].append(data["prices"][0].copy())
    report = examine(data)
    assert report["checks"]["missing_calendar_dates"] == 1
    assert report["checks"]["duplicate_price_keys"] == 1


def test_archive_does_not_fallback_to_old_complete_revision(tmp_path):
    archive = Archive(tmp_path)
    try:
        for key, modified, state in [
            ("old", "2026-10-30T00:00:00+00:00", "complete"),
            ("new", "2026-10-31T00:00:00+00:00", "pending"),
        ]:
            archive.db.execute(
                "INSERT INTO bulk_files(id,endpoint,object_key,modified,state) VALUES(?,?,?,?,?)",
                (key, "/equities/bars/daily", "equities/bars/daily/a.csv.gz", modified, state),
            )
        archive.db.commit()
    finally:
        archive.close()
    report = audit_quality(tmp_path)
    assert [r["id"] for r in report["input_manifest"]] == ["new"]
    assert any(x["problem"] == "latest_file_incomplete" for x in report["issues"])
    assert report["historical_backtest_ready"] is False


def test_archive_hash_failure_keeps_original_and_blocks_inspection(tmp_path):
    archive = Archive(tmp_path)
    raw, digest = archive.immutable(gzip.compress(b"Date,Code\n2026-10-30,01230\n"), ".csv.gz")
    path = tmp_path / "normalized" / "prices.parquet"
    path.parent.mkdir()
    path.write_bytes(b"damaged")
    try:
        archive.db.execute(
            "INSERT INTO bulk_files(id,endpoint,object_key,modified,state,raw_path,"
            "sha256,parquet_path,parquet_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "a",
                "/equities/bars/daily",
                "equities/bars/daily/a.csv.gz",
                "2026-10-31T00:00:00+00:00",
                "complete",
                raw,
                digest,
                "normalized/prices.parquet",
                "0" * 64,
            ),
        )
        archive.db.commit()
    finally:
        archive.close()
    report = audit_quality(tmp_path)
    assert any(x["problem"] == "file_hash_mismatch" for x in report["issues"])
    assert path.read_bytes() == b"damaged"


def test_report_cannot_overwrite_existing_evidence(tmp_path):
    output = tmp_path / "report.json"
    output.write_text(json.dumps({"preserve": True}))
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path), "--output", str(output)])
    assert json.loads(output.read_text()) == {"preserve": True}
