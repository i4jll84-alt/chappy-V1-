"""One-code bounded acquisition, durable pagination, and overlapping incremental updates."""

import json
import re
from datetime import date, timedelta

from trading_system.storage.store import Store, utcnow


def price_query(code: str, start: str, end: str) -> dict[str, str]:
    code = code.upper()
    if not re.fullmatch(r"[0-9][A-Z0-9]{3,4}", code):
        raise ValueError("Code must contain 4 or 5 uppercase letters/digits, starting with a digit")
    if len(code) == 4:
        code += "0"
    for value in (start, end):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Use YYYY-MM-DD dates")
        date.fromisoformat(value)
    if start > end:
        raise ValueError("Start date must not be after end date")
    return {"code": code, "from": start, "to": end}


def incremental_start(store: Store, code: str, fallback: str, end: str) -> str:
    code = price_query(code, fallback, end)["code"]
    # Query completion, not largest returned row: sparse responses are not proof of coverage.
    candidates = store.db.execute(
        "SELECT params_json FROM jobs WHERE state='complete' ORDER BY started_at DESC"
    ).fetchall()
    intervals = sorted(
        (q["from"], q["to"]) for row in candidates if (q := json.loads(row[0]))["code"] == code
    )
    watermark = None
    next_uncovered = fallback
    for start, stop in intervals:
        if stop < fallback:
            continue
        if start > next_uncovered:
            break
        if stop >= next_uncovered:
            watermark = stop
            next_uncovered = (date.fromisoformat(stop) + timedelta(days=1)).isoformat()
    if watermark is None:
        return fallback
    watermark = min(watermark, end)
    return max(fallback, (date.fromisoformat(watermark) - timedelta(days=7)).isoformat())


def ingest_prices(store: Store, client, params: dict, plan: str, refresh: bool = False) -> dict:
    params = price_query(params["code"], params["from"], params["to"])
    job = store.start(params, plan, refresh)
    job_id = job["job_id"]
    previous_callback = client.on_attempt
    client.on_attempt = lambda **event: store.attempt(job_id, **event)
    try:
        if job["state"] in {"complete", "empty", "quality_failed"}:
            # The catalog is authoritative after an interrupted pointer publication.
            manifest = store.export()
            return {
                "job_id": job_id,
                "state": job["state"],
                "cached": True,
                "rows_received": job["rows_received"],
                "parquet_generation": manifest["generation"],
                "verified_duckdb_read": True,
            }
        try:
            if job["state"] != "fetched" and job["error_category"] != "export_failed":
                for _ in range(10000):
                    request = dict(params)
                    if job["cursor"]:
                        request["pagination_key"] = job["cursor"]
                    body = client.get_page(request)
                    observed_at = utcnow()
                    raw = client.last_response_bytes
                    if raw is None:
                        raise ValueError("Original HTTP response bytes are required")
                    store.save_page(job_id, body, raw, observed_at)
                    job = store.job(job_id)
                    if job["state"] == "fetched":
                        break
                else:
                    raise ValueError("Pagination page limit exceeded")
        except Exception as exc:
            store.fail(job_id, getattr(exc, "category", "ingestion_failed"))
            raise
        try:
            return store.finish(job_id)
        except Exception:
            store.fail(job_id, "export_failed")
            raise
    finally:
        client.on_attempt = previous_callback
