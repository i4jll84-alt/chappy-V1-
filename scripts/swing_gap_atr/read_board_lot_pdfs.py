"""Extract the board-lot column from JPX's nine November 2016 monthly quotations."""

import argparse
import csv
import hashlib
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from pypdf import PdfReader

PREFIX = "https://www.jpx.co.jp/english/markets/statistics-equities/price/b5b4pj000001dldf-att/"
REQUIRED = {
    "33180",
    "41170",
    "63970",
    "72480",
    "73120",
    "82450",
    "85190",
    "86250",
    "93700",
    "97640",
}


def parse_pdf(path):
    rows = []
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    for page_id, page in enumerate(PdfReader(path).pages, 1):
        lines = page.extract_text(extraction_mode="layout").splitlines()
        assert "2016/11" in lines[0]
        header = next(line for line in lines if "売買単位" in line)
        # All issues reported in this format share a 12-character board-lot column.
        unit_offset = header.index("売買単位")
        code = None
        for line in lines:
            head = re.match(r"^\s*([0-9]{4})[\u3000 ]+(.+)", line)
            if head:
                code = head[1] + "0" if "普通株式" in head[2] else None
                name = head[2].split("普通株式")[0].strip()
                continue
            if code:
                value = line[unit_offset - 1 : unit_offset + 12].strip()
                if value in ("100", "1,000"):
                    rows.append(
                        {
                            "code": code,
                            "date": "2016-11-30",
                            "unit": int(value.replace(",", "")),
                            "name": name,
                            "page": page_id,
                            "pdf": path.name,
                            "pdf_sha256": sha,
                            "url": PREFIX + path.name,
                        }
                    )
                    code = None
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    pdfs = [args.pdf_dir / f"st{i}_201611.pdf" for i in range(1, 10)]
    with ProcessPoolExecutor(max_workers=3) as pool:
        rows = sorted(
            (r for group in pool.map(parse_pdf, pdfs) for r in group),
            key=lambda r: r["code"],
        )
    assert len(rows) == len({r["code"] for r in rows})
    assert REQUIRED.issubset({r["code"] for r in rows})
    with args.out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
