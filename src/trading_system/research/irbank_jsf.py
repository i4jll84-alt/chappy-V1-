"""Resumable IRBANK JSF archive, with shared local request limits and immutable raw pages."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from trading_system.brokers.locking import single_writer
from trading_system.storage.store import atomic_write

JST = ZoneInfo("Asia/Tokyo")
BASE_URL = "https://api.irbank.net/v1/"
BALANCES = ("loan_balance", "stock_loan_balance", "net_balance")
COUNTS = (*BALANCES, "loan_new", "loan_repay", "stock_loan_new", "stock_loan_repay")


class Halt(Exception):
    """A safe stop whose short code is suitable for logs."""


def now():
    return datetime.now(JST)


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def load_key(project):
    value = os.environ.get("IRBANK_API_KEY", "").strip()
    if not value:
        for name in (".env", ".env.local"):
            path = Path(project) / name
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if line.strip().startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().removeprefix("export ") == "IRBANK_API_KEY":
                    value = val.strip().strip(chr(34) + chr(39))
                    break
            if value:
                break
    if not value and os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, "IRBANK_API_KEY")[0]).strip()
        except OSError:
            pass
    if not value or any(c.isspace() for c in value):
        raise Halt("api_key_missing_or_invalid")
    return value


def validate(code, end, status, body):
    if not re.fullmatch(r"[0-9A-Z]{4}", code):
        raise Halt("invalid_security_code")
    date.fromisoformat(end)
    if status == 404:
        return []
    if status != 200:
        raise Halt("invalid_cached_http_status")
    data = json.loads(body)
    rows = data.get("days")
    if data.get("security_code") != code or data.get("period") != "1y":
        raise Halt("response_identity_mismatch")
    if data.get("exchange") != "XTKS" or not isinstance(rows, list) or not 0 < len(rows) <= 252:
        raise Halt("response_shape_mismatch")
    units = {r["key"]: r["unit"] for r in data["fields"]}
    if any(units.get(k) != "shares" for k in BALANCES):
        raise Halt("unexpected_balance_units")
    dates = []
    for row in rows:
        stamp = row["date"]
        date.fromisoformat(stamp)
        if stamp > end or any(type(row.get(k)) is not int for k in COUNTS):
            raise Halt("invalid_day_fields")
        if any(row[k] < 0 for k in COUNTS if k != "net_balance"):
            raise Halt("negative_balance_or_flow")
        if row["loan_balance"] - row["stock_loan_balance"] != row["net_balance"]:
            raise Halt("net_balance_mismatch")
        dates.append(stamp)
    if dates != sorted(set(dates)):
        raise Halt("duplicate_or_unsorted_dates")
    return rows


class Archive:
    def __init__(self, root):
        self.root = Path(root)

    def paths(self, code, end):
        base = self.root / "raw" / code / end
        return base / "response.json", base / "receipt.json"

    def read(self, code, end):
        raw, meta = self.paths(code, end)
        if not raw.exists() and not meta.exists():
            return None
        if not raw.exists() or not meta.exists():
            raise Halt("incomplete_cached_page_requires_review")
        receipt, body = json.loads(meta.read_bytes()), raw.read_bytes()
        if any(
            receipt.get(k) != v
            for k, v in {"code": code, "to": end, "period": "1y", "exchange": "XTKS"}.items()
        ):
            raise Halt("cached_request_mismatch")
        if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
            raise Halt("raw_hash_mismatch")
        rows = validate(code, end, receipt["http_status"], body)
        return receipt, rows

    def covering(self, code, end):
        """Reuse a validated page from another window instead of spending quota twice."""
        choices = []
        for path in (self.root / "raw" / code).glob("*/receipt.json"):
            upto = path.parent.name
            if upto < end:
                continue
            receipt, rows = self.read(code, upto)
            if rows and rows[0]["date"] <= end:
                choices.append((rows[0]["date"], receipt, rows))
        if not choices:
            return None
        _, receipt, rows = min(choices, key=lambda item: item[0])
        return receipt, rows

    def write(self, code, end, response):
        raw, meta = self.paths(code, end)
        receipt = {
            "code": code,
            "to": end,
            "period": "1y",
            "exchange": "XTKS",
            "http_status": response.status_code,
            "retrieved_at": now().isoformat(),
            "sha256": hashlib.sha256(response.content).hexdigest(),
        }
        # Persist even a malformed success for diagnosis; do not silently refetch it.
        atomic_write(raw, response.content)
        save(meta, receipt)
        return self.read(code, end)

    def merge(self, code, receipt, rows):
        path = self.root / (code + ".json")
        data = (
            json.loads(path.read_bytes())
            if path.exists()
            else {
                "code": code,
                "days": {},
                "sources": {},
                "restriction_available": False,
                "publication_time_verified": False,
            }
        )
        for row in rows:
            old = data["days"].get(row["date"])
            if old is not None and old != row:
                raise Halt("overlapping_pages_conflict")
            data["days"][row["date"]] = row
        data["sources"][receipt["to"]] = receipt
        save(path, data)


class Client:
    def __init__(self, root, key, max_requests=950, *, transport=None, pause=time.sleep, tick=now):
        if not 1 <= max_requests <= 950:
            raise ValueError("max_requests_must_be_1_to_950")
        self.root, self.pause, self.tick = Path(root), pause, tick
        self.root.mkdir(parents=True, exist_ok=True)
        self.limit, self.sent, self.last = max_requests, 0, None
        policy_path = self.root / "acquisition-policy.json"
        if not policy_path.exists():
            raise Halt("trial_policy_required")
        self.policy = json.loads(policy_path.read_bytes())
        self.starts = datetime.fromisoformat(self.policy["starts_at"])
        self.ends = datetime.fromisoformat(self.policy["ends_before"])
        if self.starts.utcoffset() is None or self.ends.utcoffset() is None:
            raise Halt("invalid_trial_policy")
        accounting = self.policy.get("quota_accounting", "all_http")
        if accounting not in ("all_http", "data_only"):
            raise Halt("invalid_quota_accounting")
        self.data_only = accounting == "data_only"
        from .irbank_budget import total_ceiling

        try:
            ceiling = total_ceiling(self.policy)
        except (ValueError, KeyError, TypeError) as exc:
            raise Halt("invalid_acquisition_extension") from exc
        if not 0 < self.policy["max_total_requests"] <= ceiling:
            raise Halt("invalid_trial_total_limit")
        self.initial_data = self.policy.get("initial_data_requests")
        if self.data_only and (
            type(self.initial_data) is not int
            or not 0 <= self.initial_data <= self.policy["initial_requests"]
        ):
            raise Halt("initial_data_count_required")
        self.run_day = self.tick().astimezone(JST).date()
        self.quota = None
        self.since_usage = 0
        self.usage_day = None
        self.http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
            timeout=45,
            follow_redirects=False,
            transport=transport,
        )
        self.db = sqlite3.connect(self.root / "requests.sqlite")
        self.db.execute("""CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY, day TEXT NOT NULL, started TEXT NOT NULL,
            kind TEXT NOT NULL, code TEXT, end_date TEXT, http_status INTEGER)""")
        self.db.commit()

    def close(self):
        self.http.close()
        self.db.close()

    def authorized_daily_cap(self, day):
        """An explicitly authorized, dated extension never raises the provider's limit."""
        extensions = self.policy.get("daily_extensions", {})
        if not isinstance(extensions, dict):
            raise Halt("invalid_daily_extension")
        extension = extensions.get(day)
        schedule = self.policy.get("daily_schedule")
        if extension is None and schedule is not None:
            if not isinstance(schedule, dict):
                raise Halt("invalid_daily_schedule")
            try:
                start = date.fromisoformat(schedule["from"])
                through = date.fromisoformat(schedule["through"])
                cap = schedule["max_data_requests"]
            except (KeyError, ValueError, TypeError) as exc:
                raise Halt("invalid_daily_schedule") from exc
            if (
                type(cap) is not int
                or not 1 <= cap <= 1000
                or not self.starts.date() <= start <= through <= self.ends.date()
            ):
                raise Halt("invalid_daily_schedule")
            if start <= date.fromisoformat(day) <= through:
                extension = {"baseline_data_attempts": 0, "max_additional_data_attempts": cap}
        if extension is None:
            return None
        if not isinstance(extension, dict):
            raise Halt("invalid_daily_extension")
        baseline = extension.get("baseline_data_attempts")
        extra = extension.get("max_additional_data_attempts")
        if (
            type(baseline) is not int
            or type(extra) is not int
            or not 0 <= baseline < 1000
            or not 0 < extra <= 1000 - baseline
        ):
            raise Halt("invalid_daily_extension")
        return baseline + extra

    def send(self, kind, code=None, end=None, *, endpoint=None, params=None):
        if self.sent >= self.limit:
            raise Halt("run_request_limit")
        if self.last is not None:
            self.pause(max(0, 1.1 - (time.monotonic() - self.last)))
        stamp = self.tick()
        if not self.starts <= stamp < self.ends:
            raise Halt("outside_free_trial_window")
        day = stamp.astimezone(JST).date().isoformat()
        if self.data_only and day != self.run_day.isoformat():
            raise Halt("day_changed_resume")
        if kind == "data" and self.usage_day != stamp.astimezone(JST).date():
            raise Halt("day_changed_resume")
        count = self.db.execute("SELECT count(*) FROM requests WHERE day=?", (day,)).fetchone()[0]
        initial_today = (
            self.policy["initial_requests"] if day == self.starts.date().isoformat() else 0
        )
        cap = self.authorized_daily_cap(day)
        if cap is None:
            if count + initial_today >= 950:
                raise Halt("local_daily_request_limit")
        elif kind == "data":
            data_count = self.db.execute(
                "SELECT count(*) FROM requests WHERE day=? AND kind='data'", (day,)
            ).fetchone()[0]
            baseline = (
                self.policy.get("daily_extensions", {})
                .get(day, {})
                .get("baseline_data_attempts", 0)
            )
            if data_count < baseline:
                raise Halt("daily_extension_ledger_mismatch")
            initial_data_today = (
                (self.initial_data if self.data_only else self.policy["initial_requests"])
                if day == self.starts.date().isoformat()
                else 0
            )
            if data_count + initial_data_today >= cap:
                raise Halt("authorized_daily_data_limit")
        if self.data_only:
            from trading_system.research.irbank_budget import data_totals

            try:
                totals = data_totals(self.policy, self.db)
            except (ValueError, KeyError, TypeError) as exc:
                raise Halt("trial_budget_ledger_invalid") from exc
            if self.policy.get("renewed_trial_budget") and day < totals["start"]:
                raise Halt("outside_free_trial_window")
            if kind == "data" and totals["budget"] >= self.policy["max_total_requests"]:
                raise Halt("trial_total_request_limit")
        else:
            total = self.db.execute("SELECT count(*) FROM requests").fetchone()[0]
            if total + self.policy["initial_requests"] >= self.policy["max_total_requests"]:
                raise Halt("trial_total_request_limit")
        cur = self.db.execute(
            "INSERT INTO requests(day,started,kind,code,end_date) VALUES(?,?,?,?,?)",
            (day, stamp.isoformat(), kind, code, end),
        )
        self.db.commit()  # Reserve before IO; interrupted/time-out attempts still count.
        self.sent += 1
        self.last = time.monotonic()
        if kind == "data":
            self.since_usage += 1
        try:
            response = self.http.get(
                "usage" if kind == "usage" else (endpoint or f"securities/{code}/jsf-margin-daily"),
                params=None
                if kind == "usage"
                else (params if endpoint else {"period": "1y", "to": end, "exchange": "XTKS"}),
            )
        except httpx.HTTPError:
            return None
        self.db.execute(
            "UPDATE requests SET http_status=? WHERE id=?", (response.status_code, cur.lastrowid)
        )
        self.db.commit()
        return response

    def refresh_usage(self):
        response = self.send("usage")
        if response is None or response.status_code != 200:
            raise Halt("usage_check_failed")
        data = response.json()
        if not all(
            type(data.get(k)) is int and data[k] >= 0 for k in ("limit", "used", "remaining")
        ):
            raise Halt("invalid_usage_response")
        if data["used"] + data["remaining"] != data["limit"]:
            raise Halt("inconsistent_usage_response")
        self.quota = data
        self.since_usage = 0
        self.usage_day = self.tick().date()
        save(self.root / "last-usage.json", dict(data, checked_at=self.tick().isoformat()))

    def get(self, code, end):
        return self.get_resource(
            f"securities/{code}/jsf-margin-daily",
            {"period": "1y", "to": end, "exchange": "XTKS"},
            code=code,
            end=end,
        )

    def get_resource(self, endpoint, params=None, code=None, end=None):
        if not re.fullmatch(
            r"(securities|industries|financials|shareholders|"
            r"securities/[0-9A-Z]{4}/(jsf-margin-daily|weekly-margin-balance))",
            endpoint,
        ):
            raise Halt("unsupported_archive_endpoint")
        consecutive_429 = 0
        for attempt in range(4):
            day = self.tick().astimezone(JST).date().isoformat()
            extra_cap = self.authorized_daily_cap(day)
            if (
                extra_cap is not None
                or self.quota is None
                or self.usage_day != self.tick().date()
                or self.since_usage >= 20
            ):
                self.refresh_usage()
            reserve = 50 if extra_cap is None else 0
            remote_cap = min(1000, self.quota["limit"]) - reserve
            if (
                self.quota["remaining"] - self.since_usage <= reserve
                or self.quota["used"] + self.since_usage >= remote_cap
            ):
                raise Halt("remote_daily_headroom")
            response = self.send("data", code, end, endpoint=endpoint, params=params)
            if response is not None and response.status_code in (200, 404):
                return response
            status = response.status_code if response is not None else 0
            if status not in (0, 429) and not 500 <= status <= 599:
                raise Halt("http_" + str(status))
            consecutive_429 = consecutive_429 + 1 if status == 429 else 0
            if consecutive_429 >= 2:
                raise Halt("repeated_429")
            if attempt == 3:
                raise Halt("retry_exhausted")
            delay = 2 ** (attempt + 1)
            if response is not None and response.headers.get("Retry-After"):
                try:
                    delay = max(delay, float(response.headers["Retry-After"]))
                except ValueError:
                    raise Halt("retry_after_requires_later_resume") from None
            if delay > 60:
                raise Halt("retry_after_requires_later_resume")
            self.pause(delay)
        raise Halt("retry_exhausted")


def work_window(archive, client, phase, code, start, end):
    path = archive.root / "state" / phase / (code + ".json")
    state = (
        json.loads(path.read_bytes())
        if path.exists()
        else {
            "code": code,
            "start": start,
            "end": end,
            "next_to": end,
            "state": "pending",
            "pages": [],
            "empty_pages": 0,
        }
    )
    if (state["code"], state["start"], state["end"]) != (code, start, end):
        raise Halt("window_identity_mismatch")
    # Reconstruct the cache after a crash; partial pages are never completion evidence.
    for page in state["pages"]:
        cached = archive.read(code, page)
        if cached is None:
            raise Halt("completed_page_missing")
        archive.merge(code, *cached)
    if state["state"] == "complete":
        return state
    while state["next_to"] >= start:
        upto = state["next_to"]
        cached = archive.read(code, upto) or archive.covering(code, upto)
        if cached is None:
            cached = archive.write(code, upto, client.get(code, upto))
        receipt, rows = cached
        archive.merge(code, receipt, rows)
        state["pages"].append(receipt["to"])
        if not rows:
            state["empty_pages"] += 1
            state["state"] = "complete"
        else:
            following = (date.fromisoformat(rows[0]["date"]) - timedelta(days=1)).isoformat()
            if following >= upto:
                raise Halt("pagination_not_advancing")
            state["next_to"] = following
            if following < start:
                state["state"] = "complete"
        save(path, state)
        if state["state"] == "complete":
            break
    return state


def run(project, root, max_requests=950, phase="audit"):
    if phase in ("first5_required", "history_required"):
        from trading_system.research.irbank_priority import run as priority_run

        return priority_run(project, root, max_requests, phase.removesuffix("_required"))
    from trading_system.research.irbank_universe import prepare

    universe = prepare(project, root)
    archive = Archive(root)
    jobs = []
    if phase in ("all", "audit"):
        jobs.extend(
            ("audit", c, universe["audit_start"], universe["audit_end"])
            for c in universe["audit_codes"]
        )
    if phase == "first5":
        from trading_system.research.irbank_required import prepare_candidates

        technical = prepare_candidates(Path(project), Path(root))
        allowed = set(universe["codes"] + universe["boundary_only_codes"])
        ordered = dict.fromkeys(
            item["code"][:-1]
            for day, pool in technical["days"].items()
            if day <= "2021-11-18"
            for item in pool
            if item["code"][:-1] in allowed
        )
        jobs.extend(("first5", c, universe["history_start"], "2021-11-19") for c in ordered)
    if phase == "history":
        jobs.extend(
            ("history", c, universe["history_start"], universe["history_end"])
            for c in sorted(set(universe["codes"] + universe["boundary_only_codes"]))
        )
    report = {
        "started_at": now().isoformat(),
        "jobs": len(jobs),
        "complete": 0,
        "empty": 0,
        "errors": 0,
        "requests_this_run": 0,
        "state": "running",
    }
    log = Path(root) / "progress.jsonl"
    with single_writer(Path(root) / "dispatch"):
        client = Client(root, load_key(project), max_requests)
        try:
            for item in jobs:
                state = work_window(archive, client, *item)
                report["complete"] += 1
                report["empty"] += int(bool(state["empty_pages"]))
                report["requests_this_run"] = client.sent
                report["remaining"] = len(jobs) - report["complete"]
                report["last"] = {"phase": item[0], "code": item[1]}
                save(Path(root) / "progress.json", report)
                line = json.dumps(report, ensure_ascii=False)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                print(line, flush=True)
            report["state"] = "complete"
        except Halt as exc:
            report["state"] = "stopped"
            report["reason"] = str(exc)
            report["errors"] += int(str(exc) in ("retry_exhausted", "overlapping_pages_conflict"))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            report["state"] = "failed"
            report["reason"] = "validation_or_storage_" + type(exc).__name__
            report["errors"] += 1
        finally:
            report["requests_this_run"] = client.sent
            report["remaining"] = len(jobs) - report["complete"]
            report["finished_at"] = now().isoformat()
            save(Path(root) / "progress.json", report)
            client.close()
    return report


def pipeline(project, root, max_requests=950):
    """Run archival and replay preparation, refusing unresolved data-definition changes."""
    from trading_system.research.irbank_audit import run as reconcile
    from trading_system.research.irbank_replay import run as replay
    from trading_system.research.irbank_required import run_needed

    project, root = Path(project), Path(root)
    report = {"state": "running", "requests_this_run": 0, "started_at": now().isoformat()}
    acquisition = run(project, root, max_requests, "audit")
    report["audit_acquisition"] = acquisition
    report["requests_this_run"] += acquisition["requests_this_run"]
    if acquisition["state"] != "complete":
        report.update(
            state=acquisition["state"], reason=acquisition.get("reason", "audit_incomplete")
        )
    else:
        audit = reconcile(project, root)
        report["audit_state"] = audit["state"]
        if audit["state"] != "checked":
            report.update(state="needs_review", reason="official_overlap_audit_not_passed")
        else:
            remaining = max_requests - report["requests_this_run"]
            if remaining:
                needed = run_needed(project, root, remaining)
                report["needed"] = needed
                report["requests_this_run"] += needed["requests_this_run"]
            lab = project.with_name(project.name + "トレード手法検討バックテスト用")
            report["replay"] = replay(project, lab, "auto")
            report.update(state="waiting", reason="historical_acquisition_or_replay_incomplete")
            remaining = max_requests - report["requests_this_run"]
            if report.get("needed", {}).get("state") == "complete" and remaining:
                extra = run(project, root, remaining, "history")
                report["history"] = extra
                report["requests_this_run"] += extra["requests_this_run"]
                if (
                    extra["state"] == "complete"
                    and report["replay"]["state"] == "complete"
                    and report["replay"].get("scope") == "full"
                ):
                    report.update(state="complete", reason=None)
    report["finished_at"] = now().isoformat()
    save(root / "pipeline-progress.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--max-requests", type=int, default=950)
    parser.add_argument(
        "--phase",
        choices=(
            "all",
            "audit",
            "needed",
            "first5",
            "history",
            "first5_required",
            "history_required",
        ),
        default="all",
    )
    opts = parser.parse_args(argv)
    project = opts.project.resolve()
    try:
        root = project / "data/irbank_jsf"
        if opts.phase == "all":
            result = pipeline(project, root, opts.max_requests)
        elif opts.phase == "needed":
            from trading_system.research.irbank_required import run_needed

            result = run_needed(project, root, opts.max_requests)
        else:
            result = run(project, root, opts.max_requests, opts.phase)
    except (Halt, ValueError) as exc:
        print(json.dumps({"state": "stopped", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["state"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
