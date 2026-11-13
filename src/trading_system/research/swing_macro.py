"""Public daily market indicators frozen for the pre-open paper assessment."""

import csv
import hashlib
import io
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree
from zipfile import BadZipFile
from zoneinfo import ZoneInfo

import httpx
from pypdf.errors import PyPdfError

from trading_system.research import swing_nikkei as nikkei
from trading_system.storage.store import atomic_write, canonical

JST = ZoneInfo("Asia/Tokyo")
NY = ZoneInfo("America/New_York")
ECB = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
MOF = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
MOF_HISTORY = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
SERIES = ("JP10Y", "DGS10", "USDJPY", "SP500", "NASDAQCOM", "NK225F")


def number(text):
    val = Decimal(text)
    if not val.is_finite():
        raise ValueError("invalid_market_value")
    return val


def observations(key, body):
    if key == "USDJPY":
        for node in ElementTree.fromstring(body).iter():
            if "time" not in node.attrib:
                continue
            currencies = {c.get("currency"): c.get("rate") for c in node}
            if "JPY" in currencies and "USD" in currencies:
                yen, dollar = (number(currencies[k]) for k in ("JPY", "USD"))
                if min(yen, dollar) <= 0:
                    raise ValueError("invalid_reference_rate")
                yield date.fromisoformat(node.attrib["time"]), yen / dollar
    elif key == "JP10Y":
        rows = csv.reader(io.StringIO(body.decode("cp932")))
        header = next((r for r in rows if r and r[0] == "基準日"), [])
        col = header.index("10年")
        eras = {"S": 1925, "H": 1988, "R": 2018}
        for r in rows:
            match = re.fullmatch(r"([SHR])(\d+)\.(\d+)\.(\d+)", r[0].strip()) if r else None
            if match and r[col].strip() not in ("", "-", "－"):
                era, y, m, d = match.groups()
                yield date(eras[era] + int(y), int(m), int(d)), number(r[col])
    else:
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
        if reader.fieldnames != ["observation_date", key]:
            raise ValueError("invalid_fred_series")
        for r in reader:
            if r[key] not in ("", ".", None):
                yield date.fromisoformat(r["observation_date"]), number(r[key])


def summarize_observations(key, rows, received):
    zone = JST if key == "JP10Y" else ZoneInfo("Europe/Berlin") if key == "USDJPY" else NY
    local = received.astimezone(zone)
    values = {}
    for d, v in rows:
        if d > local.date():
            continue
        if d == local.date() and key in ("SP500", "NASDAQCOM") and local.time() < time(16):
            continue
        if key in ("SP500", "NASDAQCOM") and v <= 0:
            raise ValueError("invalid_index_value")
        if d in values and values[d] != v:
            raise ValueError("conflicting_observation")
        values[d] = v
    if len(values) < 2:
        raise ValueError("insufficient_observations")
    prev, day = sorted(values)[-2:]
    before, now = values[prev], values[day]
    yield_series = key in ("DGS10", "JP10Y")
    age = (local.date() - day).days
    change = (now - before) * 100 if yield_series else (now / before - 1) * 100
    return dict(
        state="usable" if age <= 4 and (day - prev).days <= 7 else "stale",
        date=str(day),
        previous_date=str(prev),
        value=str(now),
        previous_value=str(before),
        unit="percent" if yield_series else "JPY_per_USD" if key == "USDJPY" else "index_points",
        change=str(change),
        change_unit="bp" if yield_series else "percent",
        age_calendar_days=age,
    )


def latest(key, body, received, other=()):
    rows = list(observations(key, body))
    for extra in other:
        rows.extend(observations(key, extra))
    return summarize_observations(key, rows, received)


def capture(root):
    started = datetime.now(UTC)
    target = started.astimezone(JST).date().isoformat()
    folder = root / "macro" / target / started.astimezone(JST).strftime("%H%M%S%f")
    start = (started.date() - timedelta(days=28)).isoformat()
    series = {}
    with httpx.Client(timeout=8) as client:
        for key in SERIES:
            url = (
                nikkei.INDEX
                if key == "NK225F"
                else ECB
                if key == "USDJPY"
                else MOF
                if key == "JP10Y"
                else (f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={key}&cosd={start}")
            )
            item = {"state": "unavailable", "url": url}
            series[key] = item
            if (root / "STOP").exists():
                item["reason"] = "stopped"
                continue
            try:
                if key == "NK225F":
                    url, body, metadata = nikkei.fetch(client, started.astimezone(JST).date())
                    received = datetime.now(UTC)
                    raw = folder / "NK225F.pdf"
                    atomic_write(raw, body)
                    item.update(
                        url=url,
                        raw=str(raw.relative_to(root)),
                        sha256=hashlib.sha256(body).hexdigest(),
                        observed_at=received.isoformat(),
                        source_published_at=None,
                        **metadata,
                    )
                    item.update(
                        nikkei.read_pdf(
                            body,
                            date.fromisoformat(metadata["report_date"]),
                            received.astimezone(JST).date(),
                        )
                    )
                    continue
                current_month = b""
                for attempt in range(2 if key == "JP10Y" else 1):
                    response = client.get(url)
                    response.raise_for_status()
                    received = datetime.now(UTC)
                    body = response.content
                    raw = folder / (
                        key
                        + ("-history" if attempt else "")
                        + (".xml" if key == "USDJPY" else ".csv")
                    )
                    atomic_write(raw, body)
                    item.update(
                        url=url,
                        raw=str(raw.relative_to(root)),
                        sha256=hashlib.sha256(body).hexdigest(),
                        observed_at=received.isoformat(),
                        source_published_at=None,
                    )
                    try:
                        item.update(latest(key, body, received, [current_month] if attempt else ()))
                        break
                    except ValueError as exc:
                        if key != "JP10Y" or attempt or str(exc) != "insufficient_observations":
                            raise
                        current_month = body
                        item["current_month"] = {
                            k: item[k] for k in ("raw", "sha256", "observed_at", "url")
                        }
                        url = MOF_HISTORY
            except (
                httpx.HTTPError,
                BadZipFile,
                PyPdfError,
                ValueError,
                IndexError,
                KeyError,
                InvalidOperation,
                ElementTree.ParseError,
            ) as exc:
                item["reason"] = type(exc).__name__
    result = dict(series=series, available_at=datetime.now(UTC).isoformat(), target=target)
    path = folder / "manifest.json"
    atomic_write(path, canonical(result).encode())
    return {
        "manifest": str(path.relative_to(root)),
        "series": {k: v["state"] for k, v in series.items()},
    }


def asof(root, cutoff):
    day = cutoff.astimezone(JST).date().isoformat()
    available_versions = []
    for path in (root / "macro" / day).glob("*/manifest.json"):
        try:
            stamp = datetime.fromisoformat(json.loads(path.read_bytes())["available_at"])
            if stamp <= cutoff:
                available_versions.append((stamp, path))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return dict(series={}, reason=type(exc).__name__)
    for _, path in sorted(available_versions, reverse=True):
        try:
            body = path.read_bytes()
            meta = json.loads(body)
            available = datetime.fromisoformat(meta["available_at"])
            if available > cutoff:
                continue
            if meta["target"] != day or set(meta["series"]) != set(SERIES):
                raise ValueError("invalid_market_snapshot")
            for k, item in meta["series"].items():
                if item["state"] == "unavailable":
                    continue
                raw = (root / item["raw"]).resolve()
                received = datetime.fromisoformat(item["observed_at"])
                if received > available or raw.parent != path.parent.resolve():
                    raise ValueError("invalid_snapshot_time_or_location")
                original = raw.read_bytes()
                if hashlib.sha256(original).hexdigest() != item["sha256"]:
                    raise ValueError("market_hash_mismatch")
                other = []
                if k == "JP10Y" and "current_month" in item:
                    extra = item["current_month"]
                    source = (root / extra["raw"]).resolve()
                    if (
                        source.parent != path.parent.resolve()
                        or datetime.fromisoformat(extra["observed_at"]) > received
                    ):
                        raise ValueError("invalid_additional_source")
                    data = source.read_bytes()
                    if hashlib.sha256(data).hexdigest() != extra["sha256"]:
                        raise ValueError("market_hash_mismatch")
                    other.append(data)
                computed = (
                    nikkei.read_pdf(
                        original,
                        date.fromisoformat(item["report_date"]),
                        received.astimezone(JST).date(),
                    )
                    if k == "NK225F"
                    else latest(k, original, received, other)
                )
                if any(item.get(name) != v for name, v in computed.items()):
                    raise ValueError("market_derived_value_mismatch")
            return dict(
                manifest=str(path.relative_to(root)),
                manifest_sha256=hashlib.sha256(body).hexdigest(),
                **meta,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            PyPdfError,
            InvalidOperation,
            ElementTree.ParseError,
        ) as exc:
            return dict(series={}, reason=type(exc).__name__)
    return dict(series={}, reason="no_snapshot_by_cutoff")


def validate_assessment(context, answer, buys):
    """Accept only a reasoned omission linked to usable, frozen observations."""
    eligible = {k for k, v in context.get("series", {}).items() if v["state"] == "usable"}
    if (
        not isinstance(answer, dict)
        or not isinstance(answer.get("reason"), str)
        or not answer["reason"].strip()
    ):
        return "missing_assessment"
    if answer.get("manifest_sha256") != context.get("manifest_sha256"):
        return "source_mismatch"
    used = answer.get("indicators")
    held = answer.get("defer")
    if not isinstance(used, list) or not all(isinstance(k, str) and k in eligible for k in used):
        return "invalid_indicators"
    if len(used) != len(set(used)) or set(used) != eligible:
        return "incomplete_indicator_review"
    if not isinstance(held, dict) or any(
        k not in buys or not isinstance(v, str) or not v.strip() for k, v in held.items()
    ):
        return "invalid_defer"
    if held and not used:
        return "no_usable_evidence"
    return None
