"""Inspect saved RSS workbook caches without starting Excel or evaluating formulas.

This intentionally supports only the three-cell connection-check workbook. It is
not a live RSS adapter and its current-date field is not a quote-date guarantee.
"""

import argparse
import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_BYTES = 16 * 1024 * 1024
ITEMS = {"A1": "現在値", "A2": "現在値詳細時刻", "A3": "現在日付"}


class SnapshotError(ValueError):
    """A fixed, non-sensitive reason why the saved cache is not accepted."""


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        entry = archive.getinfo(name)
        if entry.file_size > MAX_BYTES:
            raise SnapshotError("xml_too_large")
        raw = archive.read(entry)
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise SnapshotError("xml_declarations_not_supported")
        return ET.fromstring(raw)
    except (KeyError, ET.ParseError) as exc:
        raise SnapshotError("invalid_workbook_xml") from exc


def inspect_cache(raw: bytes, code: str, observed_at: datetime) -> dict:
    """Return cache evidence and quality issues; never infer live freshness."""
    if not re.fullmatch(r"[0-9A-Z]{4,5}\.T", code):
        raise SnapshotError("invalid_expected_code")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise SnapshotError("observation_timezone_required")
    if len(raw) > MAX_BYTES:
        raise SnapshotError("workbook_too_large")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise SnapshotError("duplicate_archive_members")
            workbook = _xml(archive, "xl/workbook.xml")
            sheets = workbook.findall("s:sheets/s:sheet", NS)
            if len(sheets) != 1 or sheets[0].get("name") != "Sheet1":
                raise SnapshotError("unexpected_sheet_layout")
            rels = _xml(archive, "xl/_rels/workbook.xml.rels")
            sheet_id = sheets[0].get(f"{{{REL_NS}}}id")
            targets = [
                rel
                for rel in rels.findall(f"{{{PACKAGE_NS}}}Relationship")
                if rel.get("Id") == sheet_id
            ]
            if (
                len(targets) != 1
                or targets[0].get("Target") != "worksheets/sheet1.xml"
                or targets[0].get("Type") != f"{REL_NS}/worksheet"
                or targets[0].get("TargetMode", "Internal") != "Internal"
            ):
                raise SnapshotError("unexpected_sheet_relationship")
            sheet = _xml(archive, "xl/worksheets/sheet1.xml")
            cells = sheet.findall("s:sheetData/s:row/s:c", NS)
            populated = [cell for cell in cells if len(cell)]
            if len(populated) != 3 or {cell.get("r") for cell in populated} != set(ITEMS):
                raise SnapshotError("unexpected_cell_layout")
            evidence = {}
            issues = []
            for cell in populated:
                address = cell.get("r")
                formula = cell.find("s:f", NS)
                expected = f'RssMarket("{code}","{ITEMS[address]}")'
                if formula is None or (formula.text or "") not in (expected, "_xll." + expected):
                    raise SnapshotError("unexpected_formula")
                value = cell.findtext("s:v", default=None, namespaces=NS)
                kind = cell.get("t", "n")
                evidence[address] = {"formula": formula.text, "type": kind, "cached_value": value}
                if value is None or value == "":
                    issues.append(f"{address}:missing_cache")
                elif kind == "e":
                    issues.append(f"{address}:excel_error")
                elif kind not in ({"n"} if address == "A1" else {"str"}):
                    issues.append(f"{address}:unexpected_cache_type")
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise SnapshotError("unreadable_workbook_archive") from exc

    normalized = {"price": None, "quote_time": None, "current_date": None}
    if not issues:
        try:
            price = Decimal(evidence["A1"]["cached_value"])
            if not price.is_finite() or price <= 0:
                raise InvalidOperation
            normalized["price"] = str(price)
        except InvalidOperation:
            issues.append("A1:invalid_price")
        time_text = evidence["A2"]["cached_value"]
        try:
            if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", time_text):
                raise ValueError
            normalized["quote_time"] = time.fromisoformat(time_text).isoformat()
        except ValueError:
            issues.append("A2:invalid_time")
        date_text = evidence["A3"]["cached_value"]
        try:
            if not re.fullmatch(r"\d{4}/\d{2}/\d{2}", date_text):
                raise ValueError
            current_date = date.fromisoformat(date_text.replace("/", "-"))
            normalized["current_date"] = current_date.isoformat()
            if current_date > observed_at.astimezone(ZoneInfo("Asia/Tokyo")).date():
                issues.append("A3:future_current_date")
        except ValueError:
            issues.append("A3:invalid_date")
    return {
        "schema_version": 1,
        "source": "rakuten_ms2_rss_saved_workbook_cache",
        "code": code,
        "sheet": "Sheet1",
        "status": "cache_valid" if not issues else "attention_required",
        "issues": issues,
        "cells": evidence,
        "normalized": normalized,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "published_at": None,
        "source_updated_at": None,
        "quote_date": None,
        "live_connection_verified": False,
        "freshness_verified": False,
    }


def capture(workbook: Path, output_root: Path, code: str) -> tuple[Path, dict]:
    """Archive exact input once per byte hash; keep separate observation reports.

    Byte identity concerns the workbook, not quote corrections. Retain invalid
    inputs too. Existing archives are checked, never overwritten.
    """
    with workbook.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SnapshotError("workbook_too_large")
    digest = hashlib.sha256(raw).hexdigest()
    raw_dir = output_root / "raw"
    reports = output_root / "observations"
    raw_dir.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{digest}.xlsx"
    try:
        with raw_path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        if raw_path.read_bytes() != raw:
            raise SnapshotError("raw_archive_mismatch") from None
    observed_at = datetime.now(UTC)
    try:
        result = inspect_cache(raw, code, observed_at)
    except SnapshotError as exc:
        result = {
            "schema_version": 1,
            "source": "rakuten_ms2_rss_saved_workbook_cache",
            "status": "rejected",
            "issues": [str(exc)],
            "observed_at": observed_at.isoformat(),
            "live_connection_verified": False,
            "freshness_verified": False,
        }
    result["raw_sha256"] = digest
    result["raw_file"] = f"raw/{digest}.xlsx"
    # No source filesystem path or file modification time is treated as market time.
    report = reports / f"{uuid.uuid4().hex}.json"
    with report.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return report, result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/rss-snapshots"))
    args = parser.parse_args(argv)
    try:
        report, result = capture(args.workbook, args.output_root, args.code)
    except (OSError, SnapshotError):
        print(json.dumps({"status": "failed", "reason": "capture_failed"}))
        return 2
    print(json.dumps({"status": result["status"], "report": str(report)}, ensure_ascii=False))
    return 0 if result["status"] == "cache_valid" else 3


if __name__ == "__main__":
    raise SystemExit(main())
