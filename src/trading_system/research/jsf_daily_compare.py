"""Versioned free JSF snapshots for the original/approximate research comparison."""

import argparse
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from trading_system.brokers.locking import single_writer
from trading_system.research import irbank_compare, swing_credit
from trading_system.storage.store import atomic_write, canonical

SCHEMA = "jsf-original-approx-v1"
MAX_DATES = 5


def application_date(value):
    compact = re.sub(r"[ /-]", "", value)
    if not re.fullmatch(r"\d{8}", compact):
        raise ValueError("invalid_jsf_application_date")
    return date.fromisoformat(f"{compact[:4]}-{compact[4:6]}-{compact[6:]}").isoformat()


def normalize(body, day):
    rows = swing_credit.records(body)
    required = {"銘柄コード", "市場区分", "制限措置等", *irbank_compare.FIELDS}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("jsf_comparison_fields_missing")
    totals, info = irbank_compare.aggregate(body)
    return {
        "date": day,
        "market_rows": rows,
        "all_market_balances": totals,
        "quality": info,
        "restriction_blank_is_research_assumption": True,
        "historical_publication_verified": False,
    }


def verify(root, manifest):
    meta = json.loads(manifest.read_bytes())
    if meta.get("schema") != SCHEMA or meta.get("state") not in ("checked", "issues_found"):
        raise ValueError("jsf_comparison_manifest_invalid")
    for relative, expected in meta["outputs"].items():
        path = (root / relative).resolve()
        if (
            not path.is_relative_to(root.resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("jsf_comparison_archive_changed")
    return meta


def collect(root, client, *, run=None, moment=None):
    root = Path(root).resolve()
    folder = root / "jsf-comparison"
    moment = moment or datetime.now(UTC)
    run = run or moment.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    with single_writer(folder / "writer.lock"):
        if (root / "STOP").exists():
            return {"schema": SCHEMA, "state": "stopped", "run": run}
        first = swing_credit.fetch(client, "GET", swing_credit.BASE)
        form = swing_credit.SearchForm(first.content)
        latest = application_date(form.inputs.get("mkYmd", ""))
        if latest > moment.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat():
            raise ValueError("jsf_future_application_date")
        known = set()
        # Revisit the latest five observed dates to retain confirmed/corrected versions.
        for path in (folder / "runs").glob("*/manifest.json"):
            previous = verify(root, path)
            known.update(d for d in previous["details"] if d <= latest)
        dates = sorted({latest, *known}, reverse=True)[:MAX_DATES]
        outputs, sources, issues, details = {}, [], [], {}
        target = folder / "runs" / run
        if target.exists():
            raise ValueError("jsf_comparison_run_exists")
        target.mkdir(parents=True)

        def keep(path, content):
            atomic_write(path, content)
            outputs[str(path.relative_to(root))] = hashlib.sha256(content).hexdigest()

        keep(target / "latest-form.html", first.content)
        for day in dates:
            if (root / "STOP").exists():
                issues.append({"date": day, "reason": "stopped"})
                break
            try:
                html, body, url = swing_credit.query(client, day, False)
                observed = datetime.now(UTC)
                # Save raw evidence even if parsing rejects the content.
                keep(target / (day + ".html"), html)
                keep(target / (day + ".csv"), body)
                parsed = normalize(body, day)
                keep(target / (day + ".json"), canonical(parsed).encode())
                sources.append(
                    {
                        "date": day,
                        "url": url,
                        "observed_at": observed.isoformat(),
                        "available_at": (observed + timedelta(seconds=1)).isoformat(),
                        "source_published_at": None,
                    }
                )
                details[day] = {
                    "market_rows": len(parsed["market_rows"]),
                    "aggregate_codes": len(parsed["all_market_balances"]),
                    **parsed["quality"],
                }
            except (ValueError, httpx.HTTPError, StopIteration, KeyError) as exc:
                issues.append({"date": day, "reason": type(exc).__name__})
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (
                    429,
                    503,
                ):
                    break
        report = {
            "schema": SCHEMA,
            "state": "checked" if not issues else "issues_found",
            "run": run,
            "checked_at": datetime.now(UTC).isoformat(),
            "latest_application_date": latest,
            "dates": dates,
            "details": details,
            "sources": sources,
            "outputs": outputs,
            "issues": issues,
            "notes": [
                "Market rows, real restrictions and rates remain unchanged in raw/normalized data.",
                "Blank restrictions apply only to the derived approximate research scenario.",
                "Latest five observed dates are refreshed; older corrections are not guaranteed.",
                "Snapshot availability starts after actual acquisition, not the application date.",
                "Static CSVs retain settlement dates and preliminary/final flags.",
            ],
        }
        atomic_write(target / "manifest.json", canonical(report).encode())
        atomic_write(folder / "status.json", canonical(report).encode())
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/free-market/public"))
    args = parser.parse_args(argv)
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=45) as client:
        report = collect(args.root, client)
    print(json.dumps({k: v for k, v in report.items() if k != "outputs"}, ensure_ascii=False))
    return 0 if report["state"] == "checked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
