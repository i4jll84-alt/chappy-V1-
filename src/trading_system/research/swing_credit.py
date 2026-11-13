"""Date-bound JSF search snapshots for the forward swing study."""

import csv
import hashlib
import io
import json
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from trading_system.brokers.locking import single_writer
from trading_system.storage.store import atomic_write, canonical

BASE = "https://www.taisyaku.jp/app/stock"
AMOUNTS = ("融資残高（株）", "貸株残高（株）", "差引残高（株）")


class SearchForm(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.inputs = {}
        self.hidden = {}
        self.actions = []
        self.links = []
        self.feed(body.decode("utf8"))

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "input" and "name" in fields:
            self.inputs[fields["name"]] = fields.get("value", "")
            if fields.get("type") == "hidden":
                self.hidden[fields["name"]] = fields.get("value", "")
        if tag == "form" and fields.get("action"):
            self.actions.append(fields["action"].split("#")[0])
        if tag == "a" and fields.get("href"):
            self.links.append(fields["href"])


def fetch(client, method, url, data=None):
    for _ in range(3):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.taisyaku.jp"
            or not parsed.path.startswith("/app/stock")
        ):
            raise ValueError("unexpected_jsf_url")
        time.sleep(0.7)
        response = client.request(method, url, data=data)
        if response.status_code not in (302, 303):
            response.raise_for_status()
            if len(response.content) > 20_000_000:
                raise ValueError("jsf_response_too_large")
            return response
        url = urljoin(url, response.headers["location"])
        method, data = "GET", None
    raise ValueError("jsf_redirect_limit")


def query(client, day, detail):
    stamp = day.replace("-", " / ")
    first = fetch(client, "GET", BASE + ("/detail/7203-01" if detail else ""))
    form = SearchForm(first.content)
    suffix = "/detail/7203/search" if detail else "/stock/search"
    action = next(u for u in form.actions if suffix in u)
    fields = {"csrf_test_name": form.inputs["csrf_test_name"]}
    if detail:
        fields.update(form.hidden)
        fields.update(mkYmdFrom=stamp, mkYmdTo=stamp, kjnYmdDays="", trjoKbn="01")
    else:
        fields.update({"mgrCd[]": "", "mgrMei": "", "mkYmd": stamp})
    result = fetch(client, "POST", urljoin(str(first.url), action), fields)
    form = SearchForm(result.content)
    if not detail and form.inputs.get("mkYmd") != stamp:
        raise ValueError("jsf_returned_wrong_date")
    link = next(u for u in form.links if u.split("/")[-1] == "csv")
    result_csv = fetch(client, "GET", urljoin(str(result.url), link))
    return result.content, result_csv.content, str(result_csv.url)


def records(body):
    return list(csv.DictReader(io.StringIO(body.decode("cp932"))))


def tokyo(body):
    rows = records(body)
    tse = {r["銘柄コード"] + "0": r for r in rows if r["市場区分"] == "東証"}
    if not tse or len({(r["銘柄コード"], r["市場区分"]) for r in rows}) != len(rows):
        raise ValueError("jsf_empty_or_duplicate")
    return tse


def capture(root, day):
    destination = root / "data/swing-forward/credit" / day
    manifest = destination / "manifest.json"
    with single_writer(destination):
        if manifest.exists():
            read_credit(root, day, datetime.now(UTC))
            return "already_acquired"
        if any(
            (root / "data" / s / "STOP").exists()
            for s in ("free-market/public", "free-market/jsf-history", "swing-forward")
        ):
            raise ValueError("stopped")
        with httpx.Client(follow_redirects=False, timeout=35) as client:
            html, body, url = query(client, day, False)
            _, detail, detail_url = query(client, day, True)
        a = tokyo(body)["72030"]
        reference = [
            r
            for r in records(detail)
            if r["申込日"] == day.replace("-", "")
            and r["銘柄コード"] == "7203"
            and r["市場区分"] == "東証"
        ]
        if len(reference) != 1 or any(not a[k].strip() or a[k] != reference[0][k] for k in AMOUNTS):
            raise ValueError("dated_credit_reference_mismatch")
        hashes = {}
        for name, content in (("form.html", html), ("all.csv", body), ("7203.csv", detail)):
            atomic_write(destination / name, content)
            hashes[name] = hashlib.sha256(content).hexdigest()
        observed = datetime.now(UTC)
        meta = dict(
            date=day,
            urls=[url, detail_url],
            hashes=hashes,
            observed_at=observed.isoformat(),
            available_at=(observed + timedelta(seconds=1)).isoformat(),
            source_published_at=None,
        )
        atomic_write(manifest, canonical(meta).encode())
        return "acquired"


def read_credit(root, day, cutoff):
    folder = root / "data/swing-forward/credit" / day
    meta = json.loads((folder / "manifest.json").read_bytes())
    if meta["date"] != day or datetime.fromisoformat(meta["available_at"]) > cutoff:
        raise ValueError("credit_unavailable_at_cutoff")
    for name in ("form.html", "all.csv", "7203.csv"):
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != meta["hashes"][name]:
            raise ValueError("credit_hash_mismatch")
    return tokyo((folder / "all.csv").read_bytes()), meta
