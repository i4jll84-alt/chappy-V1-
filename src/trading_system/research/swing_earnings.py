"""Point-in-time observations of the public JPX earnings announcement lists."""

import hashlib
import json
import re
from datetime import UTC, date, datetime
from io import BytesIO
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
from zipfile import BadZipFile
from zoneinfo import ZoneInfo

import httpx
from openpyxl import load_workbook

from trading_system.free_market import Page
from trading_system.storage.store import atomic_write, canonical

PAGE = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
JST = ZoneInfo("Asia/Tokyo")


def digest(body):
    return hashlib.sha256(body).hexdigest()


def parse_book(body):
    book = load_workbook(BytesIO(body), read_only=True, data_only=True)
    entries = []
    try:
        for sheet in book:
            columns = None
            for row in sheet.values:
                if columns is None:
                    names = [str(v).split("\n")[0].strip() for v in row]
                    if "決算発表予定日" in names and "コード" in names:
                        columns = names.index("コード"), names.index("決算発表予定日")
                    continue
                code, announced = (row[n] for n in columns)
                if code is None or str(code).strip() in ("", "コード"):
                    continue
                if not re.fullmatch("[0-9A-Z]{4}", str(code).strip()):
                    # Notes outside the table do not represent companies.
                    if isinstance(announced, date):
                        raise ValueError("invalid_company_code")
                    continue
                if isinstance(announced, datetime):
                    announced = announced.date()
                entries.append(
                    [
                        str(code).strip() + "0",
                        announced.isoformat() if isinstance(announced, date) else None,
                    ]
                )
    finally:
        book.close()
    if not entries:
        raise ValueError("empty_or_unknown_earnings_layout")
    return entries


def capture(root):
    if (root / "STOP").exists():
        return {"state": "stopped"}
    with httpx.Client(timeout=30) as client:
        page = client.get(PAGE)
        page.raise_for_status()
        parser = Page()
        parser.feed(page.content.decode("utf-8"))
        links = sorted(
            {
                urljoin(PAGE, u)
                for u in parser.links
                if urlsplit(u).path.lower().endswith((".xls", ".xlsx"))
            }
        )
        files = []
        for url in links:
            if not url.startswith(PAGE.rsplit("/", 1)[0] + "/") or not url.lower().endswith(
                ".xlsx"
            ):
                raise ValueError("unsupported_earnings_source")
            res = client.get(url)
            res.raise_for_status()
            rows = parse_book(res.content)
            files.append((url, res.content, rows))
    if not files:
        raise ValueError("no_earnings_workbook")
    now = datetime.now(UTC)
    folder = (
        root
        / "earnings"
        / now.astimezone(JST).date().isoformat()
        / (now.strftime("%H%M%S%f") + "-" + uuid4().hex[:8])
    )
    atomic_write(folder / "page.html", page.content)
    sources = []
    events = {}
    for n, (url, body, rows) in enumerate(files):
        name = f"book-{n}.xlsx"
        atomic_write(folder / name, body)
        sources.append(dict(file=name, url=url, sha256=digest(body), rows=len(rows)))
        for code, day in rows:
            events.setdefault(code, []).append([day, n])
    payload = canonical(events).encode()
    atomic_write(folder / "events.json", payload)
    available = datetime.now(UTC)
    meta = dict(
        page=PAGE,
        page_sha256=digest(page.content),
        sources=sources,
        events_sha256=digest(payload),
        observed_at=now.isoformat(),
        available_at=available.isoformat(),
        source_published_at=None,
    )
    atomic_write(folder / "manifest.json", canonical(meta).encode())
    return dict(
        state="captured",
        manifest=str((folder / "manifest.json").relative_to(root)),
        companies=len(events),
        files=len(files),
    )


def asof(root, cutoff):
    # Use only evidence acquired on the selection day before its cutoff.
    day = cutoff.astimezone(JST).date().isoformat()
    base = root / "earnings" / day
    for path in sorted(base.glob("*/manifest.json"), reverse=True):
        try:
            meta = json.loads(path.read_bytes())
            if datetime.fromisoformat(meta["available_at"]) > cutoff:
                continue
            docs = {"page.html": meta["page_sha256"], "events.json": meta["events_sha256"]}
            for source in meta["sources"]:
                if not re.fullmatch(r"book-[0-9]+\.xlsx", source["file"]):
                    raise ValueError("invalid_file")
                docs[source["file"]] = source["sha256"]
            if not meta["sources"] or any(
                digest((path.parent / file).read_bytes()) != h for file, h in docs.items()
            ):
                raise ValueError("earnings_hash_mismatch")
            events = json.loads((path.parent / "events.json").read_bytes())
            return dict(
                state="verified",
                manifest=str(path.relative_to(root)),
                manifest_sha256=digest(path.read_bytes()),
                **meta,
            ), events
        except (OSError, KeyError, ValueError, BadZipFile) as exc:
            return dict(state="unavailable", reason=str(exc), page=PAGE), {}
    return dict(state="unavailable", reason="no_snapshot_by_cutoff", page=PAGE), {}


def classify(rows, available, entry, exit_day):
    if not available:
        return {"state": "unavailable", "events": []}
    if not rows:
        return {"state": "not_listed", "events": []}
    dates = {r[0] for r in rows if r[0]}
    if len(dates) > 1 or (dates and any(r[0] is None for r in rows)):
        state = "conflicting"
    elif not dates:
        state = "date_unknown"
    else:
        day = next(iter(dates))
        if day in (entry, exit_day):
            state = "boundary_time_unknown"
        elif entry < day < exit_day:
            state = "during_hold"
        else:
            state = "outside_hold"
    return {"state": state, "events": rows}
