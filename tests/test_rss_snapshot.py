import io
import json
import zipfile
from datetime import UTC, datetime
from xml.sax.saxutils import escape

import pytest

from trading_system.data_sources.rss_snapshot import (
    NS,
    PACKAGE_NS,
    REL_NS,
    SnapshotError,
    capture,
    inspect_cache,
    main,
)

NOW = datetime(2026, 1, 5, 8, tzinfo=UTC)


def book(price="123", clock="15:30:00", day="2026/01/05", formula_code="1234.T", extra=""):
    values = [
        ("A1", "現在値", "n", price),
        ("A2", "現在値詳細時刻", "str", clock),
        ("A3", "現在日付", "str", day),
    ]
    cells = "".join(
        f'<row><c r="{address}" t="{kind}"><f>_xll.RssMarket('
        f'"{formula_code}","{item}")</f><v>{escape(value)}</v></c></row>'
        for address, item, kind, value in values
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{NS["s"]}" xmlns:r="{REL_NS}"><sheets>'
            '<sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{PACKAGE_NS}"><Relationship Id="rId1" '
            f'Type="{REL_NS}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{NS["s"]}"><sheetData>{cells}{extra}</sheetData></worksheet>',
        )
    return output.getvalue()


def test_saved_cache_does_not_prove_live_or_quote_date():
    result = inspect_cache(book(), "1234.T", NOW)
    assert result["status"] == "cache_valid"
    assert result["normalized"] == {
        "price": "123",
        "quote_time": "15:30:00",
        "current_date": "2026-01-05",
    }
    assert result["quote_date"] is None
    assert result["source_updated_at"] is None
    assert result["published_at"] is None
    assert result["freshness_verified"] is False
    assert result["live_connection_verified"] is False


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", "#N/A", ""])
def test_bad_price_is_retained_and_flagged(price):
    result = inspect_cache(book(price=price), "1234.T", NOW)
    assert result["status"] == "attention_required"
    assert result["normalized"]["price"] is None
    assert result["cells"]["A1"]["cached_value"] == (price or "")


@pytest.mark.parametrize(
    "kwargs,issue",
    [
        ({"clock": "25:00:00"}, "A2:invalid_time"),
        ({"clock": "15:30"}, "A2:invalid_time"),
        ({"day": "2026/02/30"}, "A3:invalid_date"),
        ({"day": "2026/01/06"}, "A3:future_current_date"),
    ],
)
def test_invalid_dates_and_times(kwargs, issue):
    assert issue in inspect_cache(book(**kwargs), "1234.T", NOW)["issues"]


def test_wrong_code_and_extra_cells_rejected():
    with pytest.raises(SnapshotError, match="unexpected_formula"):
        inspect_cache(book(formula_code="9999.T"), "1234.T", NOW)
    with pytest.raises(SnapshotError, match="unexpected_cell_layout"):
        inspect_cache(book(extra='<row><c r="A4"><f>OtherFunction()</f></c></row>'), "1234.T", NOW)


def test_invalid_zip_and_naive_timestamp_rejected():
    with pytest.raises(SnapshotError, match="unreadable_workbook_archive"):
        inspect_cache(b"not a workbook", "1234.T", NOW)
    with pytest.raises(SnapshotError, match="observation_timezone_required"):
        inspect_cache(book(), "1234.T", NOW.replace(tzinfo=None))


def test_repeat_capture_preserves_source_and_one_raw_archive(tmp_path):
    source = tmp_path / "source.xlsx"
    original = book()
    source.write_bytes(original)
    output = tmp_path / "out"
    first, one = capture(source, output, "1234.T")
    second, two = capture(source, output, "1234.T")
    assert first != second
    assert one["raw_sha256"] == two["raw_sha256"]
    assert len(list((output / "raw").iterdir())) == 1
    assert source.read_bytes() == original
    assert (output / one["raw_file"]).read_bytes() == original
    assert str(source) not in first.read_text(encoding="utf-8")
    # A changed saved version is retained alongside the previous version.
    source.write_bytes(book(price="124"))
    _, three = capture(source, output, "1234.T")
    assert three["raw_sha256"] != one["raw_sha256"]
    assert len(list((output / "raw").iterdir())) == 2


def test_rejected_input_still_archived(tmp_path):
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"broken")
    path, result = capture(source, tmp_path / "out", "1234.T")
    assert result["status"] == "rejected"
    assert json.loads(path.read_text(encoding="utf-8")) == result
    assert (tmp_path / "out" / result["raw_file"]).read_bytes() == b"broken"


def test_cli_reports_failure_without_source_path(tmp_path, capsys):
    missing = tmp_path / "private-name.xlsx"
    assert main(["--workbook", str(missing), "--code", "1234.T"]) == 2
    assert str(missing) not in capsys.readouterr().out
