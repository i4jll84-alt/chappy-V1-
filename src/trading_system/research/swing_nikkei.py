"""Read the front Nikkei 225 large contract from published OSE daily reports."""

import hashlib
import io
import re
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urljoin, urlsplit
from zipfile import ZipFile

from pypdf import PdfReader

BASE = "https://www.jpx.co.jp/automation/markets/statistics-derivatives/daily/"
INDEX = BASE + "json/daily_report_monthlylist.json"


def listed_reports(client, date_limit):
    reports = {}
    for month in sorted(
        {day.strftime("%Y%m") for day in (date_limit, date_limit - timedelta(days=12))}
    ):
        response = client.get(BASE + "json/daily_report_" + month + ".json")
        if response.status_code == 404:
            continue
        response.raise_for_status()
        for row in response.json()["TableDatas"]:
            day = date.fromisoformat(row["TradeDate"])
            if day <= date_limit and row.get("OseAll"):
                url = urljoin(BASE, row["OseAll"])
                if urlsplit(url).netloc != "www.jpx.co.jp" or not url.startswith(BASE + "files/"):
                    raise ValueError("unexpected_report_url")
                reports[day] = url
    return reports


def extract_pdf(blob, day):
    with ZipFile(io.BytesIO(blob)) as bundle:
        name = "sif_dyr_" + day.strftime("%Y%m%d") + ".pdf"
        (member,) = [f for f in bundle.infolist() if f.filename.split("/")[-1] == name]
        if member.file_size > 15_000_000:
            raise ValueError("oversize_index_report")
        return bundle.read(member)


def parse_night(text, day, asof_date):
    if (
        not re.search(r"(?m)^\s*Nikkei 225 Futures(?:\s{2,}|$)", text)
        or "Night Session" not in text
    ):
        raise ValueError("unexpected_futures_product")
    date_label = re.search(r"(\d{4})年\s*(\d+)月\s*(\d+)日", text)
    if not date_label or date(*map(int, date_label.groups())) != day or day > asof_date:
        raise ValueError("unexpected_trade_date")
    row = re.search(r"(?m)^\s*(\d{6})\s+(\d{2}\.\d{2})\s+(\d{9})[^\r\n]*", text)
    if not row or not row[3].endswith("0018"):
        raise ValueError("missing_front_contract")
    fields = row[0].split()
    # In this report missing prices can be displaced to the end of the extracted row.
    if (
        "…" in row[0]
        or len(fields) < 11
        or any(not re.fullmatch(r"\d{1,3}(?:,\d{3})+", x) for x in fields[3:11])
    ):
        raise ValueError("incomplete_front_contract")
    op, hi, lo, close = (Decimal(x.replace(",", "")) for x in fields[3:7])
    last = date.fromisoformat(row[1][:4] + "-" + row[2].replace(".", "-"))
    if min(op, close) < lo or max(op, close) > hi or lo <= 0 or day > last:
        raise ValueError("invalid_night_range")
    age = (asof_date - day).days
    return dict(
        state="usable" if age <= 4 and asof_date <= last else "stale",
        date=str(day),
        contract=row[1],
        code=row[3],
        session="OSE night",
        price_basis="night_close_vs_same_night_open",
        previous_date=str(day),
        previous_value=str(op),
        value=str(close),
        unit="JPY",
        change=str((close / op - 1) * 100),
        change_unit="percent",
        age_calendar_days=age,
    )


def read_pdf(body, day, asof_date):
    page = PdfReader(io.BytesIO(body)).pages[0]
    return parse_night(page.extract_text(extraction_mode="layout"), day, asof_date)


def fetch(client, asof_date):
    urls = listed_reports(client, asof_date)
    if not urls:
        raise ValueError("no_published_report")
    day = max(urls)
    response = client.get(urls[day])
    response.raise_for_status()
    raw = extract_pdf(response.content, day)
    return (
        urls[day],
        raw,
        dict(zip_sha256=hashlib.sha256(response.content).hexdigest(), report_date=str(day)),
    )
