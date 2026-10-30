"""Archive bounded, publicly linked JPX/JSF files without broker/API credentials.

CSV fields remain strings and HTML tables retain cell spans. These snapshots do
not prove historical availability, complete corporate actions, or retail fees.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from trading_system.brokers.locking import single_writer
from trading_system.storage.store import atomic_write, canonical

JPX = "https://www.jpx.co.jp"
JSF = "https://www.taisyaku.jp"
JPX_AREAS = ("rights", "ss-reg", "margin-reg", "suspended")
JSF_FILES = ("meigara", "zandaka", "shina", "seigenichiran")
SEEDS = (
    *(f"{JSF}/data/{name}.csv" for name in JSF_FILES),
    f"{JSF}/download/",
    f"{JSF}/about/loan-conditions/",
    f"{JSF}/about/loan-conditions/amendment/",
    *(f"{JPX}/markets/equities/{area}/index.html" for area in JPX_AREAS),
    f"{JPX}/markets/equities/rights/01.html",
    f"{JPX}/markets/equities/margin-reg/01.html",
    f"{JPX}/markets/equities/margin-reg/02.html",
)
SUFFIXES = (".csv", ".xls", ".xlsx", ".pdf")


def public_url(value):
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.query
        or parts.fragment
        or "%" in parts.path
        or ".." in parts.path.split("/")
    ):
        raise ValueError("public_url_not_allowed")
    allowed = False
    if parts.hostname == "www.jpx.co.jp":
        allowed = any(parts.path.startswith(f"/markets/equities/{a}/") for a in JPX_AREAS)
    elif parts.hostname == "www.taisyaku.jp":
        allowed = parts.path in {
            "/download/",
            "/about/loan-conditions/",
            "/about/loan-conditions/amendment/",
            *(f"/data/{name}.csv" for name in JSF_FILES),
        }
    if not allowed:
        raise ValueError("public_url_not_allowed")
    return value


def decode(body):
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return body.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    raise ValueError("unsupported_text_encoding")


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.options, self.headings, self.tables = [], [], [], []
        self.heading = self.cell = self.row = self.table = None
        self.table_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "option" and attrs.get("value"):
            self.options.append(attrs["value"])
        if tag in ("title", "h1", "h2", "h3", "h4"):
            self.heading = {"tag": tag, "text": ""}
        if tag == "table":
            self.table_depth += 1
            if self.table_depth == 1:
                self.table = []
        if self.table_depth == 1 and tag == "tr":
            self.row = []
        if self.table_depth == 1 and tag in ("td", "th"):
            self.cell = {
                "text": "",
                "colspan": attrs.get("colspan", "1"),
                "rowspan": attrs.get("rowspan", "1"),
            }
        if tag == "br":
            self.handle_data("\n")

    def handle_data(self, data):
        if self.heading is not None:
            self.heading["text"] += data
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if self.heading is not None and tag == self.heading["tag"]:
            self.heading["text"] = self.heading["text"].strip()
            self.headings.append(self.heading)
            self.heading = None
        if tag in ("td", "th") and self.cell is not None:
            self.cell["text"] = self.cell["text"].strip()
            if self.row is not None:
                self.row.append(self.cell)
            self.cell = None
        if tag == "tr" and self.row is not None and self.table_depth == 1:
            if self.row:
                self.table.append(self.row)
            self.row = None
        if tag == "table":
            if self.table_depth == 1 and self.table is not None:
                self.tables.append(self.table)
                self.table = None
            self.table_depth = max(0, self.table_depth - 1)


def interpret(url, body):
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in (".xls", ".xlsx", ".pdf"):
        return {"kind": "raw_only", "rows": None}, []
    text, encoding = decode(body)
    if suffix == ".csv":
        rows = list(csv.reader(io.StringIO(text)))
        return {"kind": "csv", "encoding": encoding, "rows": rows}, []
    page = Page()
    page.feed(text)
    candidates = [link for link in page.links if urlsplit(link).path.lower().endswith(SUFFIXES)]
    candidates += re.findall(r"""url:\s*['"]([^'"]+\.csv)['"]""", text)
    # Only the publisher's archive choices, never generated dates or guessed URLs.
    candidates += page.options
    links = []
    for candidate in candidates:
        target = urljoin(url, candidate)
        try:
            public_url(target)
        except ValueError:
            continue
        if target not in links:
            links.append(target)
    return {
        "kind": "html",
        "encoding": encoding,
        "headings": page.headings,
        "tables": page.tables,
        "discovered_links": links,
        "context_note": "Use source HTML for event dates and attachment associations.",
    }, links


class PublicArchive:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "public.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS observations(
                id INTEGER PRIMARY KEY, run TEXT NOT NULL, url TEXT NOT NULL,
                parent_url TEXT, state TEXT NOT NULL, observed_at TEXT NOT NULL,
                available_at TEXT, http_status INTEGER, http_last_modified TEXT,
                source_published_at TEXT, source_available_at TEXT,
                raw_path TEXT, sha256 TEXT, bytes INTEGER,
                parsed_path TEXT, parsed_sha256 TEXT, kind TEXT, error TEXT,
                UNIQUE(run,url));
        """)

    def close(self):
        self.db.close()

    def save_bytes(self, content, suffix, directory):
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / directory / digest[:2] / (digest + suffix)
        if target.exists():
            if target.read_bytes() != content:
                raise ValueError("existing_archive_corrupt")
        else:
            atomic_write(target, content)
        return str(target.relative_to(self.root)), digest

    def record(self, run, url, parent, response):
        observed = datetime.now(UTC)
        raw_path, digest = self.save_bytes(
            response.content, Path(urlsplit(url).path).suffix or ".html", "raw"
        )
        parsed, links = interpret(url, response.content)
        parsed["source_url"] = url
        parsed["raw_sha256"] = digest
        parsed_path, parsed_hash = self.save_bytes(
            canonical(parsed).encode("utf-8"), ".json", "parsed"
        )
        with self.db:
            self.db.execute(
                """INSERT INTO observations(
                    run,url,parent_url,state,observed_at,available_at,http_status,
                    http_last_modified,raw_path,sha256,bytes,parsed_path,parsed_sha256,kind)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run,
                    url,
                    parent,
                    "complete",
                    observed.isoformat(),
                    (observed + timedelta(seconds=1)).isoformat(),
                    response.status_code,
                    response.headers.get("last-modified"),
                    raw_path,
                    digest,
                    len(response.content),
                    parsed_path,
                    parsed_hash,
                    parsed["kind"],
                ),
            )
        return links

    def failure(self, run, url, parent, category, status=None):
        with self.db:
            self.db.execute(
                "INSERT INTO observations(run,url,parent_url,state,observed_at,"
                "http_status,error) VALUES(?,?,?,'failed',?,?,?)",
                (run, url, parent, datetime.now(UTC).isoformat(), status, category),
            )

    def collect(self, client, *, interval=0.6, max_documents=1200, seeds=SEEDS):
        run = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        queue, seen = [(public_url(url), None) for url in seeds], set()
        while queue:
            url, parent = queue.pop(0)
            if url in seen:
                continue
            if len(seen) >= max_documents:
                raise ValueError("document_limit_reached")
            if (self.root / "STOP").exists():
                raise ValueError("stop_requested")
            seen.add(url)
            response = None
            try:
                time.sleep(interval)
                previous = self.db.execute(
                    "SELECT * FROM observations WHERE url=? AND state='complete' "
                    "ORDER BY id DESC LIMIT 1",
                    (url,),
                ).fetchone()
                headers = {}
                if previous and previous["http_last_modified"]:
                    headers["If-Modified-Since"] = previous["http_last_modified"]
                response = client.get(url, headers=headers)
                if response.status_code == 304:
                    if not previous or not headers:
                        raise ValueError("unexpected_not_modified")
                    cached = (self.root / previous["raw_path"]).read_bytes()
                    if hashlib.sha256(cached).hexdigest() != previous["sha256"]:
                        raise ValueError("cached_raw_corrupt")
                    response = httpx.Response(
                        304,
                        content=cached,
                        headers={
                            "last-modified": response.headers.get(
                                "last-modified", previous["http_last_modified"]
                            )
                        },
                    )
                else:
                    response.raise_for_status()
                if len(response.content) > 20_000_000:
                    raise ValueError("document_too_large")
                links = self.record(run, url, parent, response)
                queue.extend((link, url) for link in links if link not in seen)
            except (httpx.HTTPError, ValueError) as exc:
                self.failure(
                    run,
                    url,
                    parent,
                    type(exc).__name__,
                    response.status_code if response is not None else None,
                )
            if response is not None and response.status_code in (429, 503):
                # Stop this pass rather than repeatedly hitting a throttled server.
                break
            if len(seen) % 25 == 0:
                print(
                    canonical({"run": run, "processed": len(seen), "remaining_links": len(queue)}),
                    flush=True,
                )
        report = self.audit(run)
        atomic_write(self.root / ("report-" + run + ".json"), canonical(report).encode())
        atomic_write(self.root / "status.json", canonical(report).encode())
        return report

    def audit(self, run):
        rows = [
            dict(row)
            for row in self.db.execute("SELECT * FROM observations WHERE run=? ORDER BY id", (run,))
        ]
        issues = []
        kinds = {}
        unchanged = 0
        added_or_changed = 0
        for row in rows:
            if row["state"] != "complete":
                issues.append(
                    {"url": row["url"], "problem": row["error"], "http_status": row["http_status"]}
                )
                continue
            kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
            previous = self.db.execute(
                "SELECT sha256 FROM observations WHERE url=? AND state='complete' "
                "AND id<? ORDER BY id DESC LIMIT 1",
                (row["url"], row["id"]),
            ).fetchone()
            if previous and previous["sha256"] == row["sha256"]:
                unchanged += 1
            else:
                added_or_changed += 1
            for path_field, hash_field in (
                ("raw_path", "sha256"),
                ("parsed_path", "parsed_sha256"),
            ):
                path = (self.root / row[path_field]).resolve()
                if not path.is_relative_to(self.root):
                    raise ValueError("archive_path_outside_root")
                if (
                    not path.exists()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != row[hash_field]
                ):
                    issues.append({"url": row["url"], "problem": path_field})
        return {
            "run": run,
            "checked_at": datetime.now(UTC).isoformat(),
            "state": "checked" if not issues else "issues_found",
            "documents": len(rows),
            "unchanged": unchanged,
            "added_or_changed": added_or_changed,
            "kinds": kinds,
            "issues": issues,
            "historical_point_in_time_verified": False,
            "complete_ten_year_history": False,
            "real_execution_enabled": False,
        }


def due_slot(moment):
    if moment.tzinfo is None:
        raise ValueError("aware_schedule_time_required")
    local = moment.astimezone(ZoneInfo("Asia/Tokyo"))
    slot = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local < slot:
        slot -= timedelta(days=1)
    while slot.weekday() >= 5:
        slot -= timedelta(days=1)
    return slot


def schedule_decision(root, moment=None):
    moment = moment or datetime.now(UTC)
    slot = due_slot(moment)
    result = {"state": "due", "checked_at": moment.isoformat(), "due_at": slot.isoformat()}
    if (root / "STOP").exists():
        return {**result, "state": "stopped"}
    for path in root.glob("report-*.json"):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("state") != "checked" or report.get("issues"):
            continue
        started = datetime.strptime(report["run"].split("-")[0], "%Y%m%dT%H%M%S").replace(
            tzinfo=UTC
        )
        completed = datetime.fromisoformat(report["checked_at"])
        # A run that started before the scheduled time must not satisfy today's slot.
        if slot <= started <= completed <= moment:
            return {**result, "state": "already_acquired", "successful_run": report["run"]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/free-market/public"))
    parser.add_argument(
        "--due",
        action="store_true",
        help="Fetch only if the latest weekday 08:00 JST slot is unfulfilled.",
    )
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    try:
        return run_public(args)
    except ValueError as exc:
        if str(exc) != "broker_state_already_open":
            raise
        print(json.dumps({"state": "already_running"}))
        return 0


def run_public(args):
    with single_writer(args.root / "writer.lock"):
        if args.due:
            decision = schedule_decision(args.root)
            if decision["state"] != "due":
                print(json.dumps(decision, ensure_ascii=False))
                return 0
        archive = PublicArchive(args.root)
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=45) as client:
                report = archive.collect(client)
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report["state"] == "checked" else 1
        finally:
            archive.close()


if __name__ == "__main__":
    raise SystemExit(main())
