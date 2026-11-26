"""Archive fixed supplementary IRBANK scopes without claiming historical PIT availability."""

import hashlib
import json
import re
from datetime import date
from pathlib import Path

import duckdb

from trading_system.brokers.locking import single_writer
from trading_system.research.irbank_jsf import Client, Halt, load_key, now, save
from trading_system.research.irbank_universe import prepare as prepare_universe
from trading_system.research.irbank_universe import sha
from trading_system.storage.store import atomic_write

PHASES = ("masters", "weekly", "financials", "shareholders")
MODE = "frozen_supplement_v1"
DOCS = {
    "weekly": "https://docs.irbank.net/reference/securities/get-security-weekly-margin-balance",
    "financials": "https://docs.irbank.net/reference/financials/get-financials",
    "shareholders": "https://docs.irbank.net/reference/shareholders/list-shareholders",
    "securities": "https://docs.irbank.net/reference/securities/list-securities",
    "industries": "https://docs.irbank.net/reference/industries/list-industries",
}


def signature(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _pinned(path):
    pin = path.with_suffix(path.suffix + ".sha256")
    if not path.exists() or not pin.exists() or sha(path) != pin.read_text().strip():
        raise Halt("supplement_manifest_pin_mismatch")
    return json.loads(path.read_bytes())


def _freeze(path, value):
    if path.exists() or path.with_suffix(path.suffix + ".sha256").exists():
        raise Halt("supplement_existing_artifact_requires_review")
    save(path, value)
    atomic_write(path.with_suffix(path.suffix + ".sha256"), sha(path).encode())


def _liquid_codes(project, universe):
    """Same-day market and turnover only; no financial outcome or profitability input."""
    history = project / "data/jquants/history"
    source = history / "quality-20261031.json"
    if sha(source) != universe["source_manifest_sha256"]:
        raise Halt("supplement_original_manifest_changed")
    quality = json.loads(source.read_bytes())
    if any(quality["checks"].values()):
        raise Halt("supplement_original_quality_failed")
    files = {"bars": [], "master": []}
    names = {"/equities/bars/daily": "bars", "/equities/master": "master"}
    found = {}
    for item in quality["input_manifest"]:
        name = names.get(item["endpoint"])
        if name is None:
            continue
        path = history / item["parquet_path"]
        relative = str(path.relative_to(project))
        digest = sha(path)
        if (
            item["state"] != "complete"
            or digest != item["parquet_sha256"]
            or digest != universe["source_sha256"].get(relative)
        ):
            raise Halt("supplement_original_source_changed")
        files[name].append(str(path))
        found[relative] = digest
    if found != universe["source_sha256"] or not all(files.values()):
        raise Halt("supplement_original_source_set_changed")
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        for name, paths in files.items():
            db.from_parquet(paths).create_view(name)
        rows = db.execute(
            """SELECT DISTINCT b.Code FROM bars b JOIN master m USING(Date,Code)
            WHERE b.Date BETWEEN ? AND ? AND m.ProdCat='011' AND right(b.Code,1)='0'
            AND m.Mkt IN ('0111','0112') AND try_cast(b.Va AS DECIMAL(23,0))>=500000000
            ORDER BY b.Code""",
            [universe["start"], universe["end"]],
        ).fetchall()
    allowed = set(universe["codes"] + universe["boundary_only_codes"])
    return [row[0][:-1] for row in rows if row[0][:-1] in allowed]


def prepare(project, root):
    """Freeze targets explicitly using local pinned originals; never calls the API."""
    project, root = Path(project), Path(root)
    path = root / "supplement/manifest.json"
    with single_writer(root / "dispatch"):
        if path.exists() or path.with_suffix(".json.sha256").exists():
            return read_manifest(root)
        universe = prepare_universe(project, root)
        codes = sorted(set(universe["codes"] + universe["boundary_only_codes"]))
        liquid = _liquid_codes(project, universe)
        jobs = {
            "weekly": [{"id": code, "code": code} for code in codes],
            "financials": [{"id": code, "code": code} for code in liquid],
            "shareholders": [{"id": code, "code": code} for code in liquid],
            "masters": [{"id": "securities"}, {"id": "industries"}],
        }
        value = {
            "mode": MODE,
            "created_at": now().isoformat(),
            "universe_sha256": sha(root / "universe.json"),
            "source_manifest_sha256": universe["source_manifest_sha256"],
            "source_sha256": universe["source_sha256"],
            "selection_period": [universe["start"], universe["end"]],
            "weekly_selection": "All original universe codes including delisted and boundary codes",
            "financials_shareholders_selection": (
                "Original universe; same-day JQ domestic common stock master Prime/Standard "
                "(0111/0112); Va >= 500000000 on at least one day in selection_period"
            ),
            "financials_scope": "annual latest 30 periods; corrected values; not historical PIT",
            "weekly_scope": "latest 52 weeks only; no historic to parameter",
            "shareholders_scope": (
                "currently effective holdings only; EDINET resolved from archived financials"
            ),
            "publication_timestamp": None,
            "historical_publication_timestamp_verified": False,
            "docs": DOCS,
            "jobs": jobs,
            "jobs_sha256": signature(jobs),
        }
        _freeze(path, value)
        return value


def read_manifest(root):
    root = Path(root)
    path = root / "supplement/manifest.json"
    value = _pinned(path)
    policy_path = root / "acquisition-policy.json"
    if policy_path.exists():
        auth = json.loads(policy_path.read_bytes()).get("supplementary_acquisition", {})
        if auth.get("enabled") and auth.get("manifest_sha256") != sha(path):
            raise Halt("supplement_authorized_manifest_mismatch")
    if value.get("mode") != MODE or value.get("jobs_sha256") != signature(value.get("jobs")):
        raise Halt("supplement_manifest_changed")
    if value.get("universe_sha256") != sha(root / "universe.json"):
        raise Halt("supplement_universe_changed")
    if set(value["jobs"]) != set(PHASES):
        raise Halt("supplement_manifest_phases_invalid")
    for jobs in value["jobs"].values():
        ids = [job["id"] for job in jobs]
        if len(ids) != len(set(ids)):
            raise Halt("supplement_duplicate_jobs")
    return value


def request_for(phase, job, archive):
    code = job.get("code")
    if code is not None and not re.fullmatch(r"[0-9A-Z]{4}", code):
        raise Halt("supplement_invalid_code")
    if phase == "weekly":
        return {"endpoint": f"securities/{code}/weekly-margin-balance", "params": {"period": "52w"}}
    if phase == "financials":
        return {
            "endpoint": "financials",
            "params": {"security_code": code, "period": "annual", "years": 30},
        }
    if phase == "shareholders":
        finance = request_for("financials", job, archive)
        cached = archive.read("financials", code, finance)
        if cached is None:
            raise Halt("supplement_financial_dependency_pending")
        receipt, data = cached
        if receipt["http_status"] == 404:
            return None
        return {"endpoint": "shareholders", "params": {"edinet_code": data["edinet_code"]}}
    if phase == "masters" and job["id"] in ("securities", "industries"):
        return {
            "endpoint": job["id"],
            "params": {"limit": 500} if job["id"] == "securities" else {},
        }
    raise Halt("supplement_invalid_phase")


def validate(request, status, body):
    if status == 404:
        return {}, [], None
    if status != 200:
        raise Halt("supplement_cached_http_error")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise Halt("supplement_response_shape")
    endpoint, params = request["endpoint"], request["params"]
    if endpoint.endswith("/weekly-margin-balance"):
        key = "weeks"
        code = endpoint.split("/")[1]
        if (
            data.get("security_code") != code
            or data.get("exchange") != "XTKS"
            or data.get("period") != "52w"
        ):
            raise Halt("supplement_weekly_identity")
    elif endpoint == "financials":
        key = "periods"
        if not re.fullmatch(r"[EG]\d{5}", str(data.get("edinet_code", ""))):
            raise Halt("supplement_financial_edinet_invalid")
    elif endpoint == "shareholders":
        key = "shareholders"
    elif endpoint in ("securities", "industries"):
        key = endpoint
    else:
        raise Halt("supplement_endpoint_invalid")
    rows = data.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise Halt("supplement_response_rows_invalid")
    if endpoint in ("financials", "shareholders") or key == "weeks":
        fields = data.get("fields")
        if not isinstance(fields, list) or any(
            not isinstance(f, dict)
            or not isinstance(f.get("key"), str)
            or not isinstance(f.get("unit"), str)
            for f in fields
        ):
            raise Halt("supplement_fields_invalid")
    if key == "weeks":
        stamps = [row.get("report_date") for row in rows]
        for stamp in stamps:
            date.fromisoformat(stamp)
        if len(rows) > 52 or stamps != sorted(set(stamps)):
            raise Halt("supplement_weekly_dates_invalid")
    if key == "periods":
        if len(rows) > 30 or any(
            not re.fullmatch(r"\d{4}/(0[1-9]|1[0-2])", str(row.get("fiscal_year", "")))
            for row in rows
        ):
            raise Halt("supplement_financial_periods_invalid")
    if endpoint == "securities":
        if len(rows) > params["limit"] or any(
            not re.fullmatch(r"[0-9A-Z]{4,5}", str(row.get("code", ""))) for row in rows
        ):
            raise Halt("supplement_securities_invalid")
        cursor = data.get("next_cursor")
        if "next_cursor" not in data or (
            cursor is not None and (not isinstance(cursor, str) or not cursor)
        ):
            raise Halt("supplement_cursor_invalid")
        if cursor is not None and (not rows or cursor == params.get("cursor")):
            raise Halt("supplement_pagination_not_advancing")
    else:
        cursor = None
        if data.get("next_cursor") is not None:
            raise Halt("supplement_undocumented_pagination")
    return data, rows, cursor


class Archive:
    def __init__(self, root, manifest_sha256):
        self.root = Path(root) / "supplement"
        self.manifest_sha256 = manifest_sha256

    def paths(self, phase, job_id, request):
        base = self.root / "raw" / phase / job_id / signature(request)
        return base / "response.json", base / "receipt.json"

    def read(self, phase, job_id, request):
        raw, meta = self.paths(phase, job_id, request)
        pin = meta.with_suffix(".json.sha256")
        if not any(path.exists() for path in (raw, meta, pin)):
            return None
        if not all(path.exists() for path in (raw, meta, pin)):
            raise Halt("supplement_incomplete_cached_page")
        receipt = _pinned(meta)
        if (
            receipt.get("request") != request
            or receipt.get("manifest_sha256") != self.manifest_sha256
            or receipt.get("phase") != phase
            or receipt.get("job_id") != job_id
        ):
            raise Halt("supplement_cached_identity_mismatch")
        if sha(raw) != receipt.get("sha256"):
            raise Halt("supplement_raw_hash_mismatch")
        data, _, _ = validate(request, receipt["http_status"], raw.read_bytes())
        return receipt, data

    def write(self, phase, job_id, request, response):
        if self.read(phase, job_id, request) is not None:
            raise Halt("supplement_cached_page_immutable")
        raw, meta = self.paths(phase, job_id, request)
        atomic_write(raw, response.content)
        _freeze(
            meta,
            {
                "request": request,
                "phase": phase,
                "job_id": job_id,
                "manifest_sha256": self.manifest_sha256,
                "http_status": response.status_code,
                "retrieved_at": now().isoformat(),
                "sha256": sha(raw),
                "publication_timestamp": None,
                "historical_publication_timestamp_verified": False,
                "current_holdings_only": phase == "shareholders",
            },
        )
        return self.read(phase, job_id, request)

    def work(self, client, phase, job):
        request = request_for(phase, job, self)
        if request is None:
            return {
                "state": "complete",
                "empty": True,
                "reason": "financial_404_edinet_unresolved",
                "pages": [],
            }
        cache_id = request["params"]["edinet_code"] if phase == "shareholders" else job["id"]
        pages, seen, count = [], set(), 0
        while True:
            identity = signature(request)
            if identity in seen:
                raise Halt("supplement_pagination_cycle")
            seen.add(identity)
            cached = self.read(phase, cache_id, request)
            if cached is None:
                response = client.get_resource(
                    request["endpoint"], request["params"], code=job.get("code")
                )
                cached = self.write(phase, cache_id, request, response)
            receipt, data = cached
            _, rows, cursor = validate(request, receipt["http_status"], json.dumps(data).encode())
            pages.append(
                {
                    "request_sha256": identity,
                    "raw_sha256": receipt["sha256"],
                    "http_status": receipt["http_status"],
                }
            )
            count += len(rows)
            if cursor is None:
                return {
                    "state": "complete",
                    "empty": count == 0,
                    "rows": count,
                    "pages": pages,
                    "cache_job_id": cache_id,
                    "code": job.get("code"),
                }
            request = {
                "endpoint": request["endpoint"],
                "params": {**request["params"], "cursor": cursor},
            }


def run(project, root, max_requests=950, phase="weekly"):
    """Resume a frozen phase under the same dispatch lock and quota ledger as JSF."""
    project, root = Path(project), Path(root)
    if phase not in PHASES:
        raise ValueError("supplement_invalid_phase")
    report = {
        "phase": phase,
        "state": "running",
        "reason": None,
        "complete": 0,
        "empty": 0,
        "remaining": 0,
        "requests_this_run": 0,
        "errors": 0,
        "started_at": now().isoformat(),
    }
    client = None
    with single_writer(root / "dispatch"):
        try:
            manifest = read_manifest(root)
            jobs = manifest["jobs"][phase]
            report.update(jobs=len(jobs), remaining=len(jobs))
            archive = Archive(root, sha(root / "supplement/manifest.json"))
            for job in jobs:
                # Cached jobs can finish even when today's quota is already exhausted.
                if client is None:

                    class DeferredClient:
                        def get_resource(self, *args, **kwargs):
                            nonlocal client
                            if client is None:
                                client = Client(root, load_key(project), max_requests)
                            return client.get_resource(*args, **kwargs)

                    worker = DeferredClient()
                else:
                    worker = client
                state = archive.work(worker, phase, job)
                save(root / "supplement/state" / phase / (job["id"] + ".json"), state)
                report["complete"] += 1
                report["empty"] += int(state["empty"])
                report["remaining"] -= 1
            report["state"] = "complete"
        except Halt as exc:
            report.update(state="stopped", reason=str(exc))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report.update(state="failed", reason="validation_or_storage_" + type(exc).__name__)
            report["errors"] += 1
        finally:
            report.update(
                requests_this_run=client.sent if client else 0, finished_at=now().isoformat()
            )
            save(root / "supplement/progress" / (phase + ".json"), report)
            if client:
                client.close()
    return report
