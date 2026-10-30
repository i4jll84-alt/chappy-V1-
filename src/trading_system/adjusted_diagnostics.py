"""Audit rounding differences using every saved API response, without network access.

The published decimal coefficients are treated as exact observations. This audit
does not infer the provider's internal coefficients or replace any price series.
"""

import argparse
import bisect
import hashlib
import json
import math
import sqlite3
from collections import Counter
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

from trading_system.adjusted_history import ADJUSTED, DAILY, RAW, positive_decimal
from trading_system.history_audit import safe_path
from trading_system.history_download import identity, now, sha_file
from trading_system.storage.store import atomic_write, canonical


def rounded(value):
    scaled = value * 10
    tenths, remainder = divmod(scaled.numerator, scaled.denominator)
    return Fraction(tenths + (2 * remainder >= scaled.denominator), 10)


def exact_text(value):
    with localcontext() as context:
        context.prec = 60
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def analyze_rows(rows, code, start, end):
    """Compute each event interval independently, then inspect every API value."""
    ordered = sorted(rows, key=lambda r: r["Date"])
    dates = [r["Date"] for r in ordered]
    if not ordered or len(set(dates)) != len(dates):
        raise ValueError("empty_or_duplicate_api_dates")
    events = []
    for row in ordered:
        stamp = row["Date"]
        if (
            row["Code"] != code
            or date.fromisoformat(stamp).isoformat() != stamp
            or not start <= stamp <= end
        ):
            raise ValueError("api_code_or_period_mismatch")
        event = row["ExRT"] or ""
        factor = Fraction(positive_decimal(str(row["AdjFactor"])))
        if event not in ("", "1", "2", "3") or (factor != 1 and not event):
            raise ValueError("unknown_adjustment_event")
        if factor != 1:
            events.append((stamp, factor, event))
    event_dates = [event[0] for event in events]
    # Direct suffix products provide an independent check of the streaming builder.
    intervals = [
        (
            math.prod((e[1] for e in events[index:]), start=Fraction(1)),
            math.prod((e[1] for e in events[index:] if e[2] != "3"), start=Fraction(1)),
        )
        for index in range(len(events) + 1)
    ]
    differences = []
    null_values = numeric_values = 0
    for row in ordered:
        missing = [row[field] is None or row[field] == "" for field in RAW]
        if any(missing) and not all(missing):
            raise ValueError("partial_market_values")
        factors = intervals[bisect.bisect_right(event_dates, row["Date"])]
        for index, field in enumerate(ADJUSTED):
            actual = row[field]  # An absent key must not become an explicit null.
            is_null = actual is None or actual == ""
            if all(missing):
                null_values += 1
                if not is_null:
                    differences.append(
                        dict(date=row["Date"], field=field, kind="null_mismatch", api=str(actual))
                    )
                continue
            numeric_values += 1
            source = Fraction(positive_decimal(str(row[RAW[index]])))
            value = source * factors[0] if index < 4 else source / factors[1]
            expected = rounded(value)
            if is_null:
                differences.append(
                    dict(date=row["Date"], field=field, kind="null_mismatch", api=None)
                )
                continue
            api_value = Fraction(Decimal(str(actual)))
            if api_value == expected:
                continue
            scaled = value * 10
            floor = scaled.numerator // scaled.denominator
            midpoint = Fraction(2 * floor + 1, 20)
            gap = abs(value - midpoint)
            difference = api_value - expected
            differences.append(
                {
                    "date": row["Date"],
                    "field": field,
                    "kind": "numeric_difference",
                    "api": str(actual),
                    "exact_decimal_calculation": exact_text(expected),
                    "signed_difference": exact_text(difference),
                    "unrounded_numerator": str(value.numerator),
                    "unrounded_denominator": str(value.denominator),
                    "midpoint_distance_numerator": str(gap.numerator),
                    "midpoint_distance_denominator": str(gap.denominator),
                    "midpoint_distance_display": exact_text(gap),
                    "one_tenth_difference": abs(difference) == Fraction(1, 10),
                }
            )
    return {
        "code": code,
        "rows": len(ordered),
        "numeric_values": numeric_values,
        "null_values": null_values,
        "state": "issues_found" if differences else "checked",
        "difference_count": len(differences),
        "differences": differences,
    }


def diagnose(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if (
        output.exists()
        or output.is_relative_to(root / "raw")
        or output.is_relative_to(root / "normalized")
    ):
        raise ValueError("new_report_outside_evidence_required")
    comparison_path = root / "comparison.json"
    body = comparison_path.read_bytes()
    comparison_hash = hashlib.sha256(body).hexdigest()
    comparison = json.loads(body)
    selected = [r["code"] for r in comparison["selection"]]
    results = {r["code"]: r for r in comparison["results"]}
    if (
        comparison["state"] not in ("checked", "issues_found")
        or not selected
        or len(set(selected)) != len(selected)
        or len(results) != len(comparison["results"])
        or set(selected) != set(results)
        or comparison["completed_codes"] != len(selected)
        or comparison["total_codes"] != len(selected)
    ):
        raise ValueError("complete_comparison_required")
    inspected = []
    with sqlite3.connect((root / "history.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        for code in selected:
            if (root / "STOP").exists():
                raise ValueError("stop_requested")
            baseline = results[code]
            job = db.execute("SELECT * FROM api_jobs WHERE id=?", (baseline["job_id"],)).fetchone()
            params = json.loads(job["params_json"]) if job else {}
            if (
                not job
                or job["state"] != "complete"
                or job["cursor"] is not None
                or job["endpoint"] != DAILY
                or params != {"code": code, "from": comparison["from"], "to": comparison["to"]}
                or identity([DAILY, params]) != job["id"]
            ):
                raise ValueError("api_job_evidence_mismatch")
            pages = db.execute(
                "SELECT * FROM api_pages WHERE job_id=? ORDER BY page", (job["id"],)
            ).fetchall()
            rows = []
            evidence = []
            for page in pages:
                raw = safe_path(root, page["raw_path"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != page["sha256"]:
                    raise ValueError("api_raw_changed")
                payload = json.loads(raw, parse_float=Decimal)
                if len(payload["data"]) != page["rows"]:
                    raise ValueError("api_page_row_count_changed")
                rows.extend(payload["data"])
                evidence.append(
                    {k: page[k] for k in ("raw_path", "sha256", "observed_at", "available_at")}
                )
            if len(rows) != job["rows"] or len(rows) != baseline["api_rows"]:
                raise ValueError("api_job_row_count_changed")
            result = analyze_rows(rows, code, comparison["from"], comparison["to"])
            result["api_pages"] = evidence
            inspected.append(result)
    if sha_file(comparison_path) != comparison_hash:
        raise ValueError("comparison_changed_during_diagnosis")
    differences = [d for r in inspected for d in r["differences"]]
    numeric = [d for d in differences if d["kind"] == "numeric_difference"]
    distances = [
        Fraction(int(d["midpoint_distance_numerator"]), int(d["midpoint_distance_denominator"]))
        for d in numeric
    ]
    report = {
        "state": "issues_found" if differences else "checked",
        "checked_at": now(),
        "scope": "all saved API codes; exact published decimal coefficients; no replacements",
        "comparison_sha256": comparison_hash,
        "algorithm_source_sha256": sha_file(Path(__file__)),
        "historical_backtest_ready": False,
        "provider_algorithm_verified": False,
        "summary": {
            "codes": len(inspected),
            "rows": sum(r["rows"] for r in inspected),
            "matched_codes": sum(r["state"] == "checked" for r in inspected),
            "different_codes": sum(r["state"] != "checked" for r in inspected),
            "numeric_values": sum(r["numeric_values"] for r in inspected),
            "null_values": sum(r["null_values"] for r in inspected),
            "differences": len(differences),
            "numeric_differences": len(numeric),
            "one_tenth_differences": sum(d["one_tenth_difference"] for d in numeric),
            "differences_by_field": dict(Counter(d["field"] for d in differences)),
            "max_midpoint_distance": exact_text(max(distances)) if distances else None,
            "min_midpoint_distance": exact_text(min(distances)) if distances else None,
        },
        "results": inspected,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("report_already_exists")
    atomic_write(output, canonical(report).encode("utf-8"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = diagnose(args.root, args.output)
    print(canonical({k: v for k, v in result.items() if k != "results"}))
    return 3 if result["state"] == "issues_found" else 0


if __name__ == "__main__":
    raise SystemExit(main())
