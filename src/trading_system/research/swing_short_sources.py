"""Dated short research inputs. Cash equity market data only."""

import csv
import hashlib
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from trading_system.brokers.locking import single_writer
from trading_system.data_sources.jquants import JQuantsClient
from trading_system.free_market import PublicArchive
from trading_system.settings import Settings, api_key
from trading_system.storage.store import atomic_write, canonical

JSF = "https://www.taisyaku.jp/data/"
SHORT_PAGE = "https://www.jpx.co.jp/markets/equities/ss-reg/"


def capture_margin(project, published):
    root = project / "data/swing-forward"
    if (root / "STOP").exists() or (project / "data/jquants/history/STOP").exists():
        return "stopped"
    dest = root / "short-credit" / published
    if (dest / "manifest.json").exists():
        margin_rows(project, published, None)
        return "already_acquired"
    secret = api_key()
    if not secret:
        return "missing_key"
    params = {"published_date": published}
    pages = []
    seen = set()
    codes = set()
    with JQuantsClient(
        secret.get_secret_value(),
        plan=Settings.load(project / "config/stage1.toml").plan,
        endpoint="margin_interest",
    ) as client:
        while True:
            data = client.get_page(params)
            if not data["data"]:
                return "empty"
            for row in data["data"]:
                if row["PubDate"] != published or row["Date"] >= published or row["Code"] in codes:
                    raise ValueError("invalid_margin_publication")
                codes.add(row["Code"])
            body = client.last_response_bytes
            digest = hashlib.sha256(body).hexdigest()
            atomic_write(dest / (digest + ".json"), body)
            pages.append(digest)
            token = data.get("pagination_key")
            if not token:
                break
            if token in seen or len(pages) >= 100:
                raise ValueError("invalid_margin_pagination")
            seen.add(token)
            params["pagination_key"] = token
    stamp = datetime.now(UTC)
    meta = dict(
        published=published,
        url=JQuantsClient.ENDPOINTS["margin_interest"],
        pages=pages,
        observed_at=stamp.isoformat(),
        available_at=(stamp + timedelta(seconds=1)).isoformat(),
    )
    atomic_write(dest / "manifest.json", canonical(meta).encode())
    return "acquired"


def margin_rows(project, published, cutoff):
    folder = project / "data/swing-forward/short-credit" / published
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        return {}, {}
    meta = json.loads(manifest.read_bytes())
    if cutoff and datetime.fromisoformat(meta["available_at"]) > cutoff:
        return {}, {}
    if meta["published"] != published or not meta["pages"]:
        raise ValueError("wrong_margin_manifest")
    rows = {}
    for digest in meta["pages"]:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid_digest")
        body = (folder / (digest + ".json")).read_bytes()
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError("margin_digest_mismatch")
        for row in json.loads(body)["data"]:
            if row["PubDate"] != published or row["Date"] >= published or row["Code"] in rows:
                raise ValueError("invalid_margin_row")
            rows[row["Code"]] = row
    return rows, meta


class PublicInputs:
    def __init__(self, project, cutoff):
        self.root = project / "data/free-market/public"
        self.cutoff = cutoff.astimezone(UTC).isoformat()
        self.refs = []
        self.db = sqlite3.connect(
            (self.root / "public.sqlite").resolve().as_uri() + "?mode=ro", uri=True
        )

    def files(self, pattern, limit=1):
        rows = self.db.execute(
            "SELECT url,raw_path,sha256,available_at FROM observations "
            "WHERE url LIKE ? AND state='complete' AND available_at<=? "
            "AND raw_path IS NOT NULL ORDER BY available_at DESC,id DESC LIMIT ?",
            (pattern, self.cutoff, limit),
        )
        for url, path, digest, stamp in rows:
            target = (self.root / path).resolve()
            if not target.is_relative_to(self.root.resolve()):
                raise ValueError("invalid_public_path")
            body = target.read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("public_digest_mismatch")
            self.refs.append(dict(url=url, sha256=digest, available_at=stamp))
            yield list(csv.reader(io.StringIO(body.decode("cp932"))))

    def restrictions(self, prior, entry):
        day = entry.replace("-", "")
        members = next(self.files(JSF + "meigara.csv"), [])
        if len(members) < 3 or members[1][3] != "貸借銘柄区分（東証）":
            return None, "membership_missing"
        eligible = {r[1] + "0" for r in members[2:] if len(r) > 3 and r[0] == day and r[3] == "1"}
        if not eligible:
            return None, "membership_wrong_date"
        membership_received = self.refs[-1]["available_at"]
        notices = next(self.files(JSF + "seigenichiran.csv"), [])
        header = next(
            (i for i, r in enumerate(notices) if r[1:3] == ["銘柄コード", "銘柄名"]), None
        )
        if header is None or self.refs[-1]["available_at"][:10] < membership_received[:10]:
            return None, "restrictions_missing"
        excluded = {r[1] + "0" for r in notices[header + 1 :] if len(r) > 1 and r[1]}
        suffix = prior.replace("-", "") + "_Stocks_Restricted_Nextday.csv"
        triggers = next(self.files(SHORT_PAGE + "%/" + suffix), None)
        if triggers is None:
            return None, "price_restrictions_missing"
        if any(len(r) < 2 or not r[0].isalnum() or len(r[0]) != 4 for r in triggers if r):
            raise ValueError("invalid_price_restrictions")
        return eligible - excluded - {r[0] + "0" for r in triggers if r}, "available"

    def loan_history(self, expected):
        by_day = {}
        for table in self.files(JSF + "zandaka.csv", 18):
            if not table:
                continue
            header = table[0]
            subset = {}
            day = None
            for fields in table[1:]:
                row = dict(zip(header, fields, strict=True))
                if row["取引所区分名"] != "東証およびＰＴＳ" or row["速報／確報"] != "確報":
                    continue
                d = row["申込日"].replace("/", "-")
                if d not in expected:
                    continue
                if day and d != day:
                    raise ValueError("mixed_balance_dates")
                day = d
                code = row["銘柄コード"] + "0"
                if code in subset:
                    raise ValueError("duplicate_balance")
                subset[code] = row
            if day:
                by_day.setdefault(day, subset)
            if len(by_day) == len(expected):
                break
        return by_day


def capture_public(project, prior):
    """Evening subset for the next preopen. Never mark the full 08:00 collection complete."""
    root = project / "data/free-market/public"
    if (root / "STOP").exists() or (project / "data/swing-forward/STOP").exists():
        return "stopped"
    urls = [JSF + k + ".csv" for k in ("meigara", "zandaka", "seigenichiran")]
    index = SHORT_PAGE + "index.html"
    suffix = "/" + prior.replace("-", "") + "_Stocks_Restricted_Nextday.csv"
    with single_writer(root / "writer.lock"):
        archive = PublicArchive(root)
        try:
            with httpx.Client(timeout=12) as client:
                run = "short-" + uuid4().hex
                for url in [*urls, index]:
                    if (root / "STOP").exists():
                        return "stopped"
                    response = client.get(url)
                    response.raise_for_status()
                    links = archive.record(run, url, None, response)
                matched = [u for u in links if u.startswith(SHORT_PAGE) and u.endswith(suffix)]
                if len(matched) != 1:
                    return "restriction_link_unavailable"
                if (root / "STOP").exists():
                    return "stopped"
                response = client.get(matched[0])
                response.raise_for_status()
                archive.record(run, matched[0], index, response)
                return "acquired"
        finally:
            archive.close()
