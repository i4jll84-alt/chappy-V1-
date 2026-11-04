import hashlib
import json
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from trading_system import research_panel as panel
from trading_system.research_workbook import NS, REL

CODES = ["7203.T", *[f"{n}.T" for n in range(1000, 1030)]]
FIELDS = (
    "現在日付 現在値 現在値詳細時刻 最良買気配値 最良売気配値 "
    "最良買気配数量 最良売気配数量 最良買気配詳細時刻 最良売気配詳細時刻 出来高"
).split()


def fixture(root):
    root.mkdir(exist_ok=True)
    main = NS["s"]
    book = ET.Element(f"{{{main}}}workbook")
    sheets = ET.SubElement(book, f"{{{main}}}sheets")
    rels = ET.Element("Relationships")
    parts = {}
    for number, name in enumerate(["気配", *CODES], 1):
        ET.SubElement(sheets, f"{{{main}}}sheet", name=name, attrib={f"{{{REL}}}id": str(number)})
        ET.SubElement(
            rels,
            "Relationship",
            Id=str(number),
            Type=REL + "/worksheet",
            Target=f"worksheets/sheet{number}.xml",
        )
        sheet = ET.Element(f"{{{main}}}worksheet")
        data = ET.SubElement(sheet, f"{{{main}}}sheetData")
        rows = {}

        def cell(address, value, formula=False):
            row = "".join(c for c in address if c.isdigit())
            if row not in rows:
                rows[row] = ET.SubElement(data, f"{{{main}}}row", r=row)
            c = ET.SubElement(rows[row], f"{{{main}}}c", r=address)
            if formula:
                ET.SubElement(c, f"{{{main}}}f").text = value
            else:
                c.set("t", "inlineStr")
                ET.SubElement(ET.SubElement(c, f"{{{main}}}is"), f"{{{main}}}t").text = value

        if number == 1:
            for col, field in enumerate(["銘柄", *FIELDS]):
                cell(f"{chr(65 + col)}1", field)
            for row, code in enumerate(CODES, 2):
                cell(f"A{row}", code)
                for col, field in enumerate(FIELDS, 66):
                    cell(f"{chr(col)}{row}", f'_xll.RssMarket("{code}","{field}")', True)
            for col, field in enumerate(["市場指標", "現在日付", "更新時刻", "現在値"]):
                cell(f"{chr(65 + col)}35", field)
            for row, code in enumerate(["N225", "TOPX"], 36):
                cell(f"A{row}", code)
                for col, field in enumerate(["現在日付", "更新時刻", "現在値"], 66):
                    cell(f"{chr(col)}{row}", f'_xll.RssIndexMarket("{code}","{field}")', True)
        else:
            cell("A1", f'_xll.RssChart(A2:G2,"{name}","1M",400)', True)
            for col, field in enumerate("日付 時刻 始値 高値 安値 終値 出来高".split()):
                cell(f"{chr(65 + col)}2", field)
        parts[f"xl/worksheets/sheet{number}.xml"] = ET.tostring(sheet)
    parts["xl/workbook.xml"] = ET.tostring(book)
    parts["xl/_rels/workbook.xml.rels"] = ET.tostring(rels)
    write(root, parts)
    (root / "panel.json").write_text(
        json.dumps(
            {
                "version": 1,
                "symbols": CODES,
                "workbook_sha256": hashlib.sha256(
                    (root / panel.BOOK_NAME).read_bytes()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return parts


def write(root, parts):
    with ZipFile(root / panel.BOOK_NAME, "w", ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)


def test_excel_save_cache_format_and_shared_strings_do_not_block_restart(tmp_path):
    parts = fixture(tmp_path)
    assert panel.configuration(tmp_path)[0] == CODES
    sheet = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
    first = sheet.find(".//s:c[@r='A1']", NS)
    first.remove(first.find("s:is", NS))
    first.set("t", "s")
    ET.SubElement(first, f"{{{NS['s']}}}v").text = "0"
    parts["xl/sharedStrings.xml"] = f'<sst xmlns="{NS["s"]}"><si><t>銘柄</t></si></sst>'.encode()
    for c in sheet.findall(".//s:c", NS):
        f = c.find("s:f", NS)
        if f is not None:
            f.text = "_xlfn.SINGLE(" + f.text + ")"
            ET.SubElement(c, f"{{{NS['s']}}}v").text = "123"
            c.set("s", "2")
    parts["xl/worksheets/sheet1.xml"] = ET.tostring(sheet)
    parts["docProps/core.xml"] = b"<updated>2026-11-04</updated>"
    write(tmp_path, parts)
    assert (
        hashlib.sha256((tmp_path / panel.BOOK_NAME).read_bytes()).hexdigest()
        != json.loads((tmp_path / "panel.json").read_text())["workbook_sha256"]
    )
    assert panel.configuration(tmp_path)[0] == CODES


@pytest.mark.parametrize(
    "change,reason",
    [
        ("symbol", "layout"),
        ("header", "layout"),
        ("sheet", "layout"),
        ("chart", "formulas"),
        ("quote", "formulas"),
        ("extra_formula", "formulas"),
    ],
)
def test_changed_contract_is_rejected_before_excel_opens(tmp_path, change, reason):
    parts = fixture(tmp_path)
    key = "xl/worksheets/sheet1.xml"
    if change == "sheet":
        key = "xl/workbook.xml"
        parts[key] = parts[key].replace(b'name="7203.T"', b'name="9999.T"')
    elif change == "chart":
        key = "xl/worksheets/sheet2.xml"
        parts[key] = parts[key].replace(b"1M", b"5M")
    else:
        sheet = ET.fromstring(parts[key])
        address = {"symbol": "A2", "header": "B1", "quote": "B2", "extra_formula": "A1"}[change]
        c = sheet.find(f".//s:c[@r='{address}']", NS)
        if change in {"symbol", "header"}:
            c.find("s:is/s:t", NS).text = "changed"
        elif change == "quote":
            c.find("s:f", NS).text = '_xll.RssMarket("9999.T","現在日付")'
        else:
            ET.SubElement(c, f"{{{NS['s']}}}f").text = "1+1"
        parts[key] = ET.tostring(sheet)
    write(tmp_path, parts)
    with pytest.raises(ValueError, match=f"research_workbook_{reason}_changed"):
        panel.configuration(tmp_path)


@pytest.mark.parametrize("part", ["xl/vbaProject.bin", "xl/externalLinks/externalLink1.xml"])
def test_unexpected_active_content_rejected(tmp_path, part):
    parts = fixture(tmp_path)
    parts[part] = b"unexpected"
    write(tmp_path, parts)
    with pytest.raises(ValueError, match="invalid_research_workbook"):
        panel.configuration(tmp_path)


def test_missing_and_corrupt_workbook_have_specific_safe_reasons(tmp_path):
    fixture(tmp_path)
    book = tmp_path / panel.BOOK_NAME
    book.unlink()
    with pytest.raises(ValueError, match="research_workbook_unreadable"):
        panel.configuration(tmp_path)
    book.write_bytes(b"not an xlsx")
    with pytest.raises(ValueError, match="invalid_research_workbook"):
        panel.configuration(tmp_path)
