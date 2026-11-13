"""Validate the research workbook's contract, independent of Excel save caches."""

import posixpath
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
FIELDS = (
    "現在日付",
    "現在値",
    "現在値詳細時刻",
    "最良買気配値",
    "最良売気配値",
    "最良買気配数量",
    "最良売気配数量",
    "最良買気配詳細時刻",
    "最良売気配詳細時刻",
    "出来高",
)
BAR_FIELDS = ("日付", "時刻", "始値", "高値", "安値", "終値", "出来高")
INDEX_FIELDS = ("現在日付", "更新時刻", "現在値")


def chart_batches(symbols):
    count = (len(symbols) + 1) // 2 if len(symbols) > 50 else len(symbols)
    return [symbols[i : i + count] for i in range(0, len(symbols), count)]


def expected_cells(symbols, paired=False):
    """Map each sheet to its fixed text cells and permitted formulas."""
    # A1 is a display caption. The reader uses the field names in B1:K1 and codes in A2:A32.
    text = {f"{chr(col)}1": field for col, field in enumerate(FIELDS, start=66)}
    formulas = {}
    for row, symbol in enumerate(symbols, 2):
        text[f"A{row}"] = symbol
        for col, field in enumerate(FIELDS, 66):
            formulas[f"{chr(col)}{row}"] = f'RssMarket("{symbol}","{field}")'
    header = len(symbols) + 4
    text.update({f"{chr(65 + j)}{header}": v for j, v in enumerate(("市場指標", *INDEX_FIELDS))})
    for row, code in enumerate(("N225", "TOPX"), header + 1):
        text[f"A{row}"] = code
        for col, field in enumerate(INDEX_FIELDS, 66):
            formulas[f"{chr(col)}{row}"] = f'RssIndexMarket("{code}","{field}")'
    result = {"気配": (text, formulas)}
    batches = chart_batches(symbols) if paired else [symbols]
    for i, symbol in enumerate(batches[0]):
        result[f"分足{i + 1}" if paired else symbol] = (
            {f"{chr(65 + j)}2": v for j, v in enumerate(BAR_FIELDS)},
            {"A1": f'RssChart(A2:G2,"{symbol}","1M",400)'},
        )
    return result


def normalize_formula(value):
    # Excel may serialize scalar XLL calls using its implicit-intersection wrapper.
    if value.startswith("_xlfn.SINGLE(") and value.endswith(")"):
        value = value[len("_xlfn.SINGLE(") : -1]
    return re.sub(r"^@?(?:_xll\.)?", "", value)


def validate_workbook(path, symbols, paired=False):
    """Inspect OOXML without opening Excel or evaluating any workbook formula."""
    try:
        with ZipFile(path) as archive:
            _validate(archive, symbols, paired)
    except OSError as exc:
        raise ValueError("research_workbook_unreadable") from exc
    except (BadZipFile, ET.ParseError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ValueError("invalid_research_workbook") from exc
    except ValueError as exc:
        if str(exc) in {
            "invalid_research_workbook",
            "research_workbook_layout_changed",
            "research_workbook_formulas_changed",
        }:
            raise
        raise ValueError("invalid_research_workbook") from exc


def _validate(archive, symbols, paired=False):
    names = archive.namelist()
    if len(names) != len(set(names)) or any(
        part.startswith(("xl/externalLinks/", "xl/activeX/", "xl/macrosheets/"))
        or "vbaproject" in part.lower()
        or part == "xl/connections.xml"
        for part in names
    ):
        raise ValueError("invalid_research_workbook")
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    for name in workbook.findall("s:definedNames/s:definedName", NS):
        if name.get("name") not in {"_xlnm.Print_Area", "_xlnm.Print_Titles"}:
            raise ValueError("invalid_research_workbook")
    rels = {r.attrib["Id"]: r for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
    strings = []
    if "xl/sharedStrings.xml" in names:
        strings = [
            "".join(item.itertext()) for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))
        ]
    expected = expected_cells(symbols, paired)
    sheets = workbook.findall("s:sheets/s:sheet", NS)
    if [s.get("name") for s in sheets] != list(expected):
        raise ValueError("research_workbook_layout_changed")
    for sheet in sheets:
        rel = rels[sheet.attrib[f"{{{REL}}}id"]]
        if rel.get("Type") != REL + "/worksheet" or rel.get("TargetMode") == "External":
            raise ValueError("invalid_research_workbook")
        target = rel.attrib["Target"]
        target = posixpath.normpath(
            target.lstrip("/") if target.startswith("/") else "xl/" + target
        )
        if not target.startswith("xl/worksheets/"):
            raise ValueError("invalid_research_workbook")
        cells = ET.fromstring(archive.read(target)).findall("s:sheetData/s:row/s:c", NS)
        by_address = {c.attrib["r"]: c for c in cells}
        if len(cells) != len(by_address):
            raise ValueError("invalid_research_workbook")
        text, formulas = expected[sheet.attrib["name"]]
        for address, wanted in text.items():
            cell = by_address.get(address)
            if cell is None:
                raise ValueError("research_workbook_layout_changed")
            value = cell.findtext("s:v", default="", namespaces=NS)
            if cell.get("t") == "s":
                value = strings[int(value)]
            elif cell.get("t") == "inlineStr":
                value = "".join(cell.find("s:is", NS).itertext())
            if value != wanted:
                raise ValueError("research_workbook_layout_changed")
        actual = {
            address: normalize_formula(cell.find("s:f", NS).text or "")
            for address, cell in by_address.items()
            if cell.find("s:f", NS) is not None
        }
        if paired and sheet.attrib["name"] != "気配":
            slot = int(sheet.attrib["name"].removeprefix("分足")) - 1
            allowed = [
                {"A1": f'RssChart(A2:G2,"{batch[slot]}","1M",400)'}
                for batch in chart_batches(symbols)
                if len(batch) > slot
            ]
            if actual not in allowed:
                raise ValueError("research_workbook_formulas_changed")
        elif actual != formulas:
            raise ValueError("research_workbook_formulas_changed")
