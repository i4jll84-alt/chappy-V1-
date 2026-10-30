"""Compare a frozen adjusted snapshot with official daily-bar API responses.

Samples are chosen by fixed source-data rules before viewing API results.
Original responses and checkpoints use the existing archive transaction model.
"""

import argparse
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb

from trading_system.adjusted_history import ADJUSTED, DAILY, RAW
from trading_system.brokers.locking import single_writer
from trading_system.data_sources.jquants import JQuantsClient
from trading_system.history_audit import safe_path
from trading_system.history_download import Archive, identity, now, sha_file
from trading_system.ingestion.prices import price_query
from trading_system.settings import api_key
from trading_system.storage.store import atomic_write, canonical


def equal_value(a, b, numeric=True):
    if a is None or a == "":
        return b is None or b == ""
    if b is None or b == "" or isinstance(a, bool) or isinstance(b, bool):
        return False
    if not numeric:
        return a == b
    try:
        left, right = Decimal(str(a)), Decimal(str(b))
        return left.is_finite() and right.is_finite() and left == right
    except InvalidOperation:
        return False


def compare_rows(local, official):
    def keyed(rows):
        result = {}
        for row in rows:
            key = (row["Code"], row["Date"])
            if key in result:
                raise ValueError("duplicate_comparison_key")
            result[key] = row
        return result

    expected, actual = keyed(local), keyed(official)
    common = sorted(expected.keys() & actual.keys())
    result = {
        "local_rows": len(expected),
        "api_rows": len(actual),
        "compared_rows": len(common),
        "missing_api_keys": sorted(expected.keys() - actual.keys()),
        "unexpected_api_keys": sorted(actual.keys() - expected.keys()),
        "raw_differences": 0,
        "adjusted_differences": 0,
        "difference_examples": [],
    }
    for key in common:
        for field in (*RAW, "AdjFactor", "ExRT", *ADJUSTED):
            # Missing keys are distinct from explicit nulls in an API record.
            if (
                field not in actual[key]
                or field not in expected[key]
                or not equal_value(
                    expected[key].get(field), actual[key].get(field), numeric=field != "ExRT"
                )
            ):
                group = "adjusted_differences" if field in ADJUSTED else "raw_differences"
                result[group] += 1
                if len(result["difference_examples"]) < 20:
                    result["difference_examples"].append(
                        {
                            "code": key[0],
                            "date": key[1],
                            "field": field,
                            "local": str(expected[key].get(field)),
                            "api": str(actual[key].get(field)),
                        }
                    )
    result["state"] = (
        "checked"
        if (
            common
            and not result["missing_api_keys"]
            and not result["unexpected_api_keys"]
            and not result["raw_differences"]
            and not result["adjusted_differences"]
        )
        else "issues_found"
    )
    return result


def choose_codes(db, start=None, end=None):
    if start is None and end is None:
        prefix = "WITH candidate_prices AS (SELECT * FROM prices) "
        bounds = []
    elif start is not None and end is not None:
        prefix = (
            "WITH eligible_codes AS (SELECT DISTINCT Code FROM prices "
            "WHERE Date BETWEEN ? AND ?), candidate_prices AS "
            "(SELECT p.* FROM prices p SEMI JOIN eligible_codes e USING(Code)) "
        )
        bounds = [start, end]
    else:
        raise ValueError("both_comparison_bounds_required")
    selected = {}
    for event, code in db.execute(
        prefix + "SELECT ExRT,Code FROM candidate_prices WHERE try_cast(AdjFactor AS DOUBLE)<>1 "
        "GROUP BY ExRT,Code QUALIFY row_number() OVER(PARTITION BY ExRT "
        "ORDER BY sha256(Code),Code)<=2 ORDER BY ExRT,Code",
        bounds,
    ).fetchall():
        selected.setdefault(code, []).append("event_" + event + "_hash_sample")
    for label, query in (
        (
            "most_adjustments",
            "SELECT Code FROM candidate_prices WHERE try_cast(AdjFactor AS DOUBLE)<>1 "
            "GROUP BY Code ORDER BY count(*) DESC,Code LIMIT 1",
        ),
        (
            "no_adjustments",
            "SELECT Code FROM candidate_prices GROUP BY Code HAVING "
            "count(*) FILTER(WHERE try_cast(AdjFactor AS DOUBLE)<>1)=0 "
            "ORDER BY sha256(Code),Code LIMIT 1",
        ),
    ):
        for (code,) in db.execute(prefix + query, bounds).fetchall():
            selected.setdefault(code, []).append(label)
    return [{"code": code, "reasons": reasons} for code, reasons in sorted(selected.items())]


def choose_precision_codes(db, start, end):
    rows = db.execute(
        "WITH eligible AS (SELECT DISTINCT Code FROM prices WHERE Date BETWEEN ? AND ?) "
        "SELECT p.Code,count(*) FROM prices p SEMI JOIN eligible e USING(Code) "
        "WHERE try_cast(AdjFactor AS DOUBLE)<>1 "
        "AND length(split_part(AdjFactor,'.',2))>12 GROUP BY p.Code ORDER BY p.Code",
        [start, end],
    ).fetchall()
    return [
        {"code": code, "reasons": ["long_decimal_coefficient_review"], "event_rows": count}
        for code, count in rows
    ]


class PricePages:
    def __init__(self, secret):
        self.client = JQuantsClient(secret, plan="standard")
        # Ten extra requests/minute leave headroom beside the 100/min history job.
        self.client._interval = 6.0

    def get(self, endpoint, params):
        if endpoint != DAILY:
            raise ValueError("comparison_endpoint_not_allowed")
        body = self.client.get_page(params)
        return body, self.client.last_response_bytes

    def close(self):
        self.client.close()


def verify(root, derived, output, start, client_factory=PricePages, precision_review=False):
    root, derived, output = Path(root).resolve(), Path(derived).resolve(), Path(output).resolve()
    manifest_path = derived / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("state") != "complete":
        raise ValueError("derived_snapshot_incomplete")
    target = safe_path(derived, manifest["file"])
    if sha_file(target) != manifest["sha256"]:
        raise ValueError("derived_file_changed")
    end = manifest["anchor_date"]
    if start.isoformat() > end:
        raise ValueError("invalid_comparison_period")
    inputs = manifest["inputs"]
    for item in inputs:
        if sha_file(safe_path(root, item["parquet_path"])) != item["parquet_sha256"]:
            raise ValueError("source_snapshot_changed")
    if (
        output == root
        or output.is_relative_to(root)
        or output == derived
        or output.is_relative_to(derived)
    ):
        raise ValueError("separate_verification_directory_required")
    mode = "precision_review" if precision_review else "fixed_samples"
    marker = output / "selection.json"
    if output.exists() and any(output.iterdir()):
        if not marker.is_file():
            raise ValueError("unrecognized_verification_directory")
        prior = json.loads(marker.read_text(encoding="utf-8"))
        if (
            prior.get("derived_manifest_sha256") != sha_file(manifest_path)
            or prior.get("from") != start.isoformat()
            or prior.get("to") != end
            or prior.get("mode", "fixed_samples") != mode
        ):
            raise ValueError("verification_inputs_changed")
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "state": "running",
        "mode": mode,
        "started_at": now(),
        "from": start.isoformat(),
        "to": end,
        "derived_manifest_sha256": sha_file(manifest_path),
        "historical_point_in_time_verified": False,
        "historical_backtest_ready": False,
        "real_execution_enabled": False,
        "selection_rule": (
            "nonunit_coefficients_over_12_decimal_places_with_period_rows"
            if precision_review
            else "fixed_event_samples_with_rows_in_comparison_period_v2"
        ),
        "scope": "selected codes against saved API responses; observation times retained",
    }
    with single_writer(output):
        archive = Archive(output)
        client = None
        try:
            with duckdb.connect() as db:
                db.read_parquet(
                    [str(safe_path(root, r["parquet_path"])) for r in inputs]
                ).create_view("prices")
                db.read_parquet(str(target)).create_view("adjusted")
                if db.execute("SELECT max(Date) FROM prices").fetchone()[0] != end:
                    raise ValueError("comparison_requires_latest_snapshot_anchor")
                selector = choose_precision_codes if precision_review else choose_codes
                report["selection"] = selector(db, start.isoformat(), end)
                report.update(completed_codes=0, total_codes=len(report["selection"]))
                # Save the selection before making a network request.
                atomic_write(output / "selection.json", canonical(report).encode("utf-8"))
                secret = api_key()
                if secret is None:
                    raise ValueError("api_key_unavailable")
                client = client_factory(secret.get_secret_value())
                results = []
                for selection in report["selection"]:
                    if (output / "STOP").exists():
                        raise ValueError("stop_requested")
                    params = price_query(selection["code"], start.isoformat(), end)
                    job_id = identity([DAILY, params])
                    with archive.db:
                        archive.db.execute(
                            "INSERT OR IGNORE INTO api_jobs(id,endpoint,params_json,state) "
                            "VALUES(?,?,?,'pending')",
                            (job_id, DAILY, canonical(params)),
                        )
                    while True:
                        if (output / "STOP").exists():
                            raise ValueError("stop_requested")
                        job = archive.db.execute(
                            "SELECT * FROM api_jobs WHERE id=?", (job_id,)
                        ).fetchone()
                        if job["state"] == "complete":
                            break
                        archive.fetch_json_page(client, job)
                    pages = archive.db.execute(
                        "SELECT * FROM api_pages WHERE job_id=? ORDER BY page", (job_id,)
                    ).fetchall()
                    official = []
                    for page in pages:
                        raw = safe_path(output, page["raw_path"])
                        if sha_file(raw) != page["sha256"]:
                            raise ValueError("api_raw_changed")
                        official.extend(json.loads(raw.read_bytes(), parse_float=Decimal)["data"])
                    fields = ",".join("a." + f for f in ADJUSTED)
                    cursor = db.execute(
                        f"SELECT p.*,{fields} FROM prices p JOIN adjusted a USING(Date,Code) "
                        "WHERE p.Code=? AND p.Date BETWEEN ? AND ? ORDER BY p.Date",
                        [selection["code"], start.isoformat(), end],
                    )
                    names = [c[0] for c in cursor.description]
                    local = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
                    expected_count = db.execute(
                        "SELECT count(*) FROM prices WHERE Code=? AND Date BETWEEN ? AND ?",
                        [selection["code"], start.isoformat(), end],
                    ).fetchone()[0]
                    if len(local) != expected_count:
                        raise ValueError("derived_join_lost_rows")
                    result = compare_rows(local, official)
                    result.update(
                        code=selection["code"],
                        job_id=job_id,
                        pages=len(pages),
                        first_observed_at=min(p["observed_at"] for p in pages),
                        last_observed_at=max(p["observed_at"] for p in pages),
                    )
                    results.append(result)
                    report["results"] = results
                    report["completed_codes"] = len(results)
                    atomic_write(output / "comparison.json", canonical(report).encode("utf-8"))
                    print(
                        canonical(
                            {
                                "code": selection["code"],
                                "state": result["state"],
                                "rows": result["compared_rows"],
                            }
                        ),
                        flush=True,
                    )
            if sha_file(target) != manifest["sha256"]:
                raise ValueError("derived_changed_during_comparison")
            for item in inputs:
                if sha_file(safe_path(root, item["parquet_path"])) != item["parquet_sha256"]:
                    raise ValueError("source_changed_during_comparison")
            report.update(
                state="checked"
                if results and all(r["state"] == "checked" for r in results)
                else "issues_found",
                completed_at=now(),
                compared_rows=sum(r["compared_rows"] for r in results),
                raw_differences=sum(r["raw_differences"] for r in results),
                adjusted_differences=sum(r["adjusted_differences"] for r in results),
            )
        except Exception as exc:
            report.update(
                state="stopped" if str(exc) == "stop_requested" else "failed",
                error_type=type(exc).__name__,
                completed_at=now(),
            )
            atomic_write(output / "comparison.json", canonical(report).encode("utf-8"))
            raise
        finally:
            if client:
                client.close()
            archive.close()
    atomic_write(output / "comparison.json", canonical(report).encode("utf-8"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/jquants/history"))
    parser.add_argument("--derived", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 10, 31))
    parser.add_argument("--precision-review", action="store_true")
    args = parser.parse_args(argv)
    result = verify(
        args.root, args.derived, args.output, args.start, precision_review=args.precision_review
    )
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, ensure_ascii=False))
    return 0 if result["state"] == "checked" else 3


if __name__ == "__main__":
    raise SystemExit(main())
