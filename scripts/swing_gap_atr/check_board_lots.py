"""Verify both historical lot sources and coverage of every ranked order."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from board_lots import BoardLots


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def audit(lots_path, pdf_csv, candidates, path):
    lots = BoardLots(lots_path)
    pdf_rows = rows(pdf_csv)
    overlap = []
    for r in pdf_rows:
        unit = lots.unit(r["code"], r["date"])
        if unit:
            assert unit == int(r["unit"]), r
            overlap.append(r["code"])
    candidate_counts = Counter(lots.unit(r["code"], r["entry"]) for r in rows(candidates))
    assert candidate_counts == {100: 11785, 1000: 271}
    assert lots.unit("18680", "2018-10-01") == 1000
    assert lots.unit("18680", "2018-10-11") == 1000
    assert lots.unit("18680", "2018-10-12") is None
    for code, before, after in (
        ("47720", "2017-06-27", "2017-06-28"),
        ("69980", "2017-09-26", "2017-09-27"),
    ):
        assert lots.unit(code, before) == 1000
        assert lots.unit(code, after) == 100
    by_evidence = Counter(r["evidence"] for r in rows(lots_path))
    source_pdfs = {r["pdf"]: {"sha256": r["pdf_sha256"], "url": r["url"]} for r in pdf_rows}
    record = {
        "pdf_rows_extracted": len(pdf_rows),
        "cross_section_comparisons": len(overlap),
        "cross_section_mismatches": 0,
        "candidate_lot_counts": candidate_counts,
        "candidates_unknown": candidate_counts[None],
        "intervals_by_evidence": by_evidence,
        "archived_originals": source_pdfs,
        "rank_sha256": hashlib.sha256(candidates.read_bytes()).hexdigest(),
        "lookup_sha256": hashlib.sha256(lots_path.read_bytes()).hexdigest(),
        "pypdf_extraction_sha256": hashlib.sha256(pdf_csv.read_bytes()).hexdigest(),
        "pdf_archive_page": "https://www.jpx.co.jp/markets/statistics-equities/price/00-archives-10.html",
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("lots", "pdf-csv", "candidates", "out"):
        parser.add_argument("--" + arg, type=Path, required=True)
    args = parser.parse_args()
    audit(args.lots, args.pdf_csv, args.candidates, args.out)
