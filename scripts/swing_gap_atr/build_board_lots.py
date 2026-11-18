"""Build historical exchange board lots from JPX's official all-issuer change list."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import duckdb
import xlrd

XLS_URL = (
    "https://www.jpx.co.jp/equities/improvements/unit/um3qrc000001s91k-att/nlsgeu000001nx7a.xls"
)
XLS_SHA = "996a1f70161b0774f0e6ae7f2c7e9332238ab7be1860aa3d50c6ae7b924cfed9"
ANCHOR = "2018-10-01"
START = "2016-10-03"
ANCHOR_PAGE = "https://www.jpx.co.jp/equities/improvements/unit/03.html"
EXCEPTION = "https://www.jpx.co.jp/news/1021/20180926-01.html"
EXCEPTION_DELISTING = "https://www.jpx.co.jp/news/1021/20180926-13.html"


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def changes(path):
    if digest(path) != XLS_SHA:
        raise ValueError("Unexpected historical JPX spreadsheet")
    book = xlrd.open_workbook(path)
    sheet = book.sheet_by_name("一覧")
    assert sheet.row_values(3)[3:6] == ["効力発生日 ", "売買単位の変更日", "変更内容"]
    entries = {}
    for idx in range(5, sheet.nrows):
        _, _name, number, _statutory, first_trade, text, _note = sheet.row_values(idx)
        code = f"{int(number):04d}0"
        assert code not in entries
        old, new = map(int, text.split("→"))
        assert new == 100
        first = xlrd.xldate_as_datetime(first_trade, book.datemode).date().isoformat()
        entries[code] = (first, old, new)
    assert len(entries) == 1539
    return entries


def master_segments(project):
    root = project / "data/jquants/history"
    manifest = root / "quality-20261031.json"
    quality = json.loads(manifest.read_bytes())
    versions = {}
    for item in quality["input_manifest"]:
        if item["endpoint"] != "/equities/master":
            continue
        month = Path(item["object_key"]).name.split("_")[-1].split(".")[0]
        if not ("201610" <= month <= "201810"):
            continue
        rel = item["parquet_path"]
        if item["state"] != "complete" or digest(root / rel) != item["parquet_sha256"]:
            raise ValueError(f"Unverified master archive: {rel}")
        versions[rel] = item["parquet_sha256"]
    assert len(versions) == 25
    with duckdb.connect() as con:
        con.from_parquet([str(root / rel) for rel in versions]).create_view("listing")
        groups = con.execute(
            """
            WITH date_order AS (
                SELECT Date, dense_rank() OVER (ORDER BY Date) AS market_seq
                FROM (SELECT DISTINCT Date FROM listing WHERE Date <= ?)
            ), membership AS (
                SELECT Code, Date,
                       market_seq - row_number() OVER (PARTITION BY Code ORDER BY Date) AS segment
                FROM listing JOIN date_order USING (Date)
                WHERE Mkt IN ('0101','0102','0104','0106','0107') AND ProdCat='011'
            )
            SELECT Code, min(Date), max(Date) FROM membership
            GROUP BY Code, segment ORDER BY Code, min(Date)
        """,
            [ANCHOR],
        ).fetchall()
    return groups, versions


def generate(project, xls, csv_path, evidence_path, supplemental=None):
    entries = changes(xls)
    segments, source_hashes = master_segments(project)
    extra = {}
    if supplemental:
        with supplemental.open(encoding="utf-8-sig", newline="") as handle:
            extra = {row["code"]: row for row in csv.DictReader(handle)}
    intervals = []
    for code, start, end in segments:
        event = entries.get(code)
        # A membership gap can correspond to a different issuer with the same code.
        # Use an event inside this segment, or a pre-archive event for a continuous
        # listing already present at START, or the complete-list anchor for survivors.
        if event and (
            start <= event[0] <= end or start == START and event[0] < start or end == ANCHOR
        ):
            change, old, new = event
            before = (date.fromisoformat(change) - timedelta(days=1)).isoformat()
            if start < change:
                intervals.append((code, start, min(before, end), old, "jpx_change_list"))
            if change <= end:
                intervals.append((code, max(start, change), end, new, "jpx_change_list"))
        elif end == ANCHOR:
            unit = 1000 if code == "18680" else 100
            intervals.append((code, start, end, unit, "jpx_unification_anchor"))
        elif code in extra and start <= extra[code]["date"] <= end:
            info = extra[code]
            intervals.append((code, start, end, int(info["unit"]), info["url"]))
        else:
            intervals.append((code, start, end, None, "uncertain"))
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "start", "end", "unit", "evidence"])
        writer.writerows(intervals)
    evidence = {
        "spreadsheet_url": XLS_URL,
        "spreadsheet_sha256": XLS_SHA,
        "records": len(entries),
        "landing": ANCHOR_PAGE,
        "exception": EXCEPTION,
        "exception_delisting": EXCEPTION_DELISTING,
        "unit_change_column": "売買単位の変更日",
        "anchor_date": ANCHOR,
        "lookup_sha256": digest(csv_path),
        "interval_counts": Counter(row[-1] for row in intervals),
        "first_master": START,
        "master_sha256": source_hashes,
        "supplemental_sha256": digest(supplemental) if supplemental else None,
    }
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return intervals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--xls", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--supplemental", type=Path)
    args = parser.parse_args()
    generate(args.project, args.xls, args.out, args.evidence, args.supplemental)
