"""Offline preparation of evidence-linked rights, dividend and cost observations.

This command never enables historical execution. It preserves missing inputs and
publishes a new, immutable bundle only after reading and checking its sources.
PDF text is a pinned extraction, not a substitute for human validation of a PDF.
"""

import argparse
import csv
import io
import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from fractions import Fraction
from html.parser import HTMLParser
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from trading_system.backtest.historical_evidence import (
    JSF_POLICY,
    JSF_POLICY_URL,
    EvidenceTime,
    SplitRatio,
    ordinary_share_candidate,
    parse_reverse_fee,
    prepared_available_at,
)
from trading_system.history_audit import safe_path
from trading_system.history_download import sha_file
from trading_system.storage.store import canonical

VERSION = "historical_observations_v2"
SUPPORTED_VERSIONS = {"historical_observations_v1", VERSION}
CODE = r"[0-9][0-9A-Z][0-9][0-9A-Z](?:[0-9])?"
RIGHT = re.compile(
    rf"(?<![0-9A-Z])({CODE})\s+.*?(\d{{4}}\.\d{{2}}\.\d{{2}})\s+"
    r"(\d{4}\.\d{2}\.\d{2})\s+([0-9,]+(?:\.\d+)?):([0-9,]+(?:\.\d+)?)\s*(\D.*)$"
)
RIGHT_WITHOUT_RATIO = re.compile(
    rf"(?<![0-9A-Z])({CODE})\s+.*?(\d{{4}}\.\d{{2}}\.\d{{2}})\s+"
    r"(\d{4}\.\d{2}\.\d{2})\s+(新株予約権(?:の株主|の)?無償割当て)\s*$"
)

LOT = re.compile(
    rf"(\d{{4}}\.\d{{2}}\.\d{{2}})\s+({CODE})\s+.*?"
    r"([0-9,]+)株\s*[（(]([0-9,]+)株[）)]"
)
JSF_COLUMNS = [
    "銘柄コード",
    "銘柄名",
    "市場区分",
    "貸借区分",
    "基準日",
    "制限措置等",
    "融資残高（株）",
    "貸株残高（株）",
    "差引残高（株）",
    "最高料率（円）",
    "最低料率（円）",
    "品貸料率（円）",
    "品貸料率（年率換算/％）",
    "応札ランク",
]
DIVIDEND_COLUMNS = [
    "DiscDate",
    "DiscTime",
    "Code",
    "DiscNo",
    "DocType",
    "CurPerType",
    "CurPerSt",
    "CurPerEn",
    "CurFYSt",
    "CurFYEn",
    "Div1Q",
    "Div2Q",
    "Div3Q",
    "DivFY",
    "DivAnn",
    "DivUnit",
    "FDiv1Q",
    "FDiv2Q",
    "FDiv3Q",
    "FDivFY",
    "FDivAnn",
    "FDivUnit",
    "NxFDiv1Q",
    "NxFDiv2Q",
    "NxFDiv3Q",
    "NxFDivFY",
    "NxFDivAnn",
    "NxFDivUnit",
]


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return datetime.fromisoformat(value) if value else None


def evidence(meta):
    return EvidenceTime(
        timestamp(meta["observed_at"]),
        timestamp(meta.get("source_published_at")),
        timestamp(meta["available_at"]),
    )


def write_new(path, value):
    with path.open("xb") as stream:
        stream.write(canonical(value).encode("utf-8"))


class Sources:
    def __init__(self):
        self.items = {}
        self.times = []

    def pin(self, root, relative, expected=None):
        path = safe_path(Path(root).resolve(), relative)
        digest = sha_file(path)
        if expected is not None and digest != expected:
            raise ValueError("source_hash_mismatch: " + str(path))
        key = str(path)
        if key in self.items and self.items[key]["sha256"] != digest:
            raise ValueError("source_changed_during_preparation")
        self.items[key] = {"path": key, "sha256": digest, "bytes": path.stat().st_size}
        return path

    def json(self, root, relative):
        return json.loads(self.pin(root, relative).read_bytes())

    def timed(self, meta):
        item = evidence(meta)
        self.times.append(item)
        return item.usable_at.isoformat()

    def recheck(self):
        for item in self.items.values():
            if sha_file(Path(item["path"])) != item["sha256"]:
                raise ValueError("source_changed_during_preparation")


def fraction_text(value):
    return f"{value.numerator}/{value.denominator}"


def parse_right(line, page, source_id, usable_at):
    match = RIGHT.search(line)
    if match is None:
        match = RIGHT_WITHOUT_RATIO.search(line)
        if match is None:
            return None
        code, ex_day, record_day, label = match.groups()
        before = after = None
    else:
        code, ex_day, record_day, before, after, label = match.groups()
    ex_day = date.fromisoformat(ex_day.replace(".", "-")).isoformat()
    record_day = date.fromisoformat(record_day.replace(".", "-")).isoformat()
    if record_day < ex_day:
        raise ValueError("record_date_before_ex_date")
    if before is not None:
        before, after = (Fraction(v.replace(",", "")) for v in (before, after))
        if before <= 0 or after <= 0:
            raise ValueError("positive_ratio_required")
    label = label.strip()
    kind = {"分割": "split", "株式分割": "split", "株式併合": "consolidation"}.get(label)
    ratio = SplitRatio(kind, before, after) if kind else None
    if label.startswith("新株予約権"):
        kind = "subscription_rights_allotment"
    elif label == "株式無償割当て":
        kind = "free_share_allotment"
    elif label in {"株主割当", "株主割当て"}:
        kind = "shareholder_subscription"
    return {
        "code": code if len(code) == 5 else code + "0",
        "ex_date": ex_day,
        "record_date": record_day,
        "legal_effective_date": None,
        "payment_date": None,
        "event_text": label,
        "kind": kind or "unsupported_allocation_or_product",
        "ratio_before": fraction_text(before) if before is not None else None,
        "ratio_after": fraction_text(after) if after is not None else None,
        "ratio_stated": before is not None,
        "price_multiplier": fraction_text(ratio.price_multiplier) if ratio else None,
        "quantity_multiplier": fraction_text(ratio.quantity_multiplier) if ratio else None,
        "source_id": source_id,
        "page": page,
        "source_line": line,
        "source_available_at": usable_at,
        "human_validated": False,
        "usable_as_execution_action": False,
    }


def months_between(start, end):
    result = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        result.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def monthly_observations(research, public, sources, start, end):
    coverage = sources.json(research, "monthly-coverage-v2.json")
    wanted = set(months_between(start, end))
    manifest, rights, lots, unparsed = [], [], [], []
    keys, events, lot_keys = set(), set(), set()
    for item in sorted(coverage["manifest"], key=lambda v: (v["month"], v["kind"])):
        if item["month"] not in wanted:
            continue
        key = (item["month"], item["kind"])
        if key in keys or item["kind"] not in {"kenri", "idou", "kabusu"}:
            raise ValueError("invalid_monthly_manifest")
        keys.add(key)
        sources.pin(public, item["raw_path"], item["sha256"])
        usable = sources.timed(item)
        source_id = f"jpx:{item['kind']}:{item['month']}:{item['sha256']}"
        manifest.append({**item, "source_id": source_id, "source_published_at": None})
        prefix = {"kenri": "rights-text-v2-", "idou": "changes-text-"}.get(item["kind"])
        if prefix is None:
            continue
        text = sources.json(research, prefix + item["month"] + ".json")
        if text["source"] != item:
            raise ValueError("text_source_metadata_mismatch")
        pages = text["pages"]
        if [p["page"] for p in pages] != list(range(1, len(pages) + 1)) or not pages:
            raise ValueError("text_page_sequence_invalid")
        for page in pages:
            for line in page["text"].splitlines():
                if item["kind"] == "kenri":
                    row = parse_right(line, page["page"], source_id, usable)
                    if row is None:
                        if re.search(r"[0-9]:[0-9]", line):
                            unparsed.append(
                                {"source_id": source_id, "page": page["page"], "line": line}
                            )
                        continue
                    event_key = (row["code"], row["ex_date"])
                    if event_key in events:
                        raise ValueError("duplicate_corporate_action_requires_review")
                    events.add(event_key)
                    rights.append(row)
                else:
                    match = LOT.search(line)
                    if match:
                        day, code, new, old = match.groups()
                        code = code if len(code) == 5 else code + "0"
                        day = date.fromisoformat(day.replace(".", "-")).isoformat()
                        old, new = (int(v.replace(",", "")) for v in (old, new))
                        if min(old, new) <= 0 or (code, day) in lot_keys:
                            raise ValueError("invalid_or_duplicate_lot_change")
                        lot_keys.add((code, day))
                        lots.append(
                            {
                                "code": code,
                                "change_date": day,
                                "old_lot": old,
                                "new_lot": new,
                                "source_id": source_id,
                                "page": page["page"],
                                "source_line": line,
                                "source_available_at": usable,
                                "initial_state_verified": False,
                                "human_validated": False,
                            }
                        )
    if not manifest:
        raise ValueError("monthly_sources_empty")
    missing = [
        {"month": month, "kind": kind}
        for month in sorted(wanted)
        for kind in ("kenri", "idou", "kabusu")
        if (month, kind) not in keys
    ]
    return rights, lots, manifest, missing, unparsed


def archive_files(root, sources):
    database = Path(root).resolve() / "history.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM (SELECT *,row_number() OVER(PARTITION BY endpoint,object_key "
                "ORDER BY julianday(modified) DESC,rowid DESC) rank FROM bulk_files) "
                "WHERE rank=1 AND endpoint IN "
                "('/equities/bars/daily','/equities/master','/fins/summary','/markets/calendar')"
            )
        ]
    grouped = {}
    for item in rows:
        if item["state"] != "complete":
            raise ValueError("latest_bulk_version_incomplete")
        for field, hash_field in (("raw_path", "sha256"), ("parquet_path", "parquet_sha256")):
            sources.pin(root, item[field], item[hash_field])
        if pq.read_metadata(Path(root) / item["parquet_path"]).num_rows != item["rows"]:
            raise ValueError("parquet_row_count_mismatch")
        item["source_available_at"] = sources.timed(item)
        grouped.setdefault(item["endpoint"], []).append(item)
    for endpoint in (
        "/equities/bars/daily",
        "/equities/master",
        "/fins/summary",
        "/markets/calendar",
    ):
        if not grouped.get(endpoint):
            raise ValueError("required_bulk_endpoint_missing")
    return grouped


def verify_jsf_calendar(root, plan, grouped, sources):
    matches = [
        v for v in grouped["/markets/calendar"] if v["parquet_sha256"] == plan["calendar_sha256"]
    ]
    if len(matches) != 1:
        raise ValueError("jsf_calendar_version_not_available")
    path = sources.pin(root, matches[0]["parquet_path"], plan["calendar_sha256"])
    expected = [
        r["Date"]
        for r in pq.read_table(path, columns=["Date", "HolDiv"]).to_pylist()
        if plan["start"] <= r["Date"] <= plan["end"] and r["HolDiv"] in {"1", "2"}
    ]
    if expected != plan["dates"]:
        raise ValueError("jsf_plan_missing_calendar_sessions")


def match_rights(rights, grouped, root, sources, start, end):
    lookup = {(v["code"], v["ex_date"]): v for v in rights}
    rows = []
    file_ids = {
        str((Path(root) / v["parquet_path"]).resolve()).replace("\\", "/"): v
        for items in grouped.values()
        for v in items
    }
    with duckdb.connect() as db:
        for endpoint, view in (("/equities/bars/daily", "bars"), ("/equities/master", "master")):
            paths = [str((Path(root) / v["parquet_path"]).resolve()) for v in grouped[endpoint]]
            db.read_parquet(paths, filename=True).create_view(view)
        cursor = db.execute(
            """
            SELECT b.Date,b.Code,b.ExRT,b.AdjFactor,m.ProdCat,b.filename,m.filename
            FROM bars b LEFT JOIN master m ON b.Date=m.Date AND b.Code=m.Code
            WHERE b.Date BETWEEN ? AND ? AND (b.ExRT<>'' OR cast(b.AdjFactor AS DOUBLE)<>1)
            ORDER BY b.Date,b.Code
        """,
            [start.isoformat(), end.isoformat()],
        )
        keys = set()
        for day, code, exrt, factor, product, price_path, master_path in cursor.fetchall():
            key = (code, day)
            if key in keys:
                raise ValueError("duplicate_price_event_or_master")
            keys.add(key)
            refs = [file_ids[p.replace("\\", "/")] for p in (price_path, master_path) if p]
            candidate = lookup.get(key)
            state = "monthly_event_missing"
            if candidate:
                expected_type = {"split": "1", "consolidation": "2"}.get(candidate["kind"])
                if expected_type is None:
                    state = "unsupported_event_kind"
                elif exrt != expected_type or (Fraction(factor) < 1) != (expected_type == "1"):
                    state = "event_type_or_direction_conflict"
                else:
                    state = "code_date_type_direction_match"
                candidate["jquants_match"] = state
                candidate["same_date_product_category"] = product
                candidate["ordinary_share_candidate"] = ordinary_share_candidate(code, product)
            row = {
                "code": code,
                "ex_date": day,
                "exrt": exrt,
                "raw_adj_factor": factor,
                "same_date_product_category": product,
                "ordinary_share_candidate": ordinary_share_candidate(code, product),
                "match_state": state,
                "monthly_source_id": candidate["source_id"] if candidate else None,
                "price_source_id": refs[0]["id"],
                "master_source_id": refs[1]["id"] if len(refs) > 1 else None,
                "source_available_at": max(
                    [v["source_available_at"] for v in refs]
                    + ([candidate["source_available_at"]] if candidate else [])
                ),
            }
            rows.append(row)
        for row in rights:
            row.setdefault("jquants_match", "price_event_missing")
            row.setdefault("same_date_product_category", None)
            row.setdefault("ordinary_share_candidate", False)
    return rows


def dividend_observations(grouped, root, output, start, end):
    schema = pa.schema(
        [(c, pa.string()) for c in DIVIDEND_COLUMNS]
        + [
            ("source_id", pa.string()),
            ("source_row", pa.int64()),
            ("source_available_at", pa.string()),
            ("payment_date", pa.string()),
        ]
    )
    total = 0
    with pq.ParquetWriter(output, schema, compression="zstd") as writer:
        for item in sorted(grouped["/fins/summary"], key=lambda v: v["object_key"]):
            table = pq.read_table(Path(root) / item["parquet_path"], columns=DIVIDEND_COLUMNS)
            rows = []
            for number, row in enumerate(table.to_pylist(), 1):
                if start.isoformat() <= row["DiscDate"] <= end.isoformat():
                    rows.append(
                        {
                            **row,
                            "source_id": item["id"],
                            "source_row": number,
                            "source_available_at": item["source_available_at"],
                            "payment_date": None,
                        }
                    )
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
            total += len(rows)
    if not total:
        raise ValueError("dividend_disclosures_empty")
    return total


class ApplicationDate(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "mkYmd":
            self.values.append(attrs.get("value"))


def jsf_observations(root, sources, output, start, end):
    plan = sources.json(root, "plan.json")
    status = sources.json(root, "status.json")
    days = plan["dates"]
    if status["state"] != "checked" or days != sorted(set(days)) or not days:
        raise ValueError("jsf_history_not_complete")
    if [p.stem for p in sorted((Path(root) / "days").glob("*.json"))] != days:
        raise ValueError("jsf_day_manifest_coverage_mismatch")
    schema = pa.schema(
        [(c, pa.string()) for c in JSF_COLUMNS]
        + [
            ("application_date", pa.string()),
            ("source_id", pa.string()),
            ("source_row", pa.int64()),
            ("fee_state", pa.string()),
            ("total_fee_per_share", pa.string()),
        ]
    )
    counts, manifests = Counter(), []
    with pq.ParquetWriter(output, schema, compression="zstd") as writer:
        for day in days:
            if not start.isoformat() <= day <= end.isoformat():
                continue
            item = sources.json(root, "days/" + day + ".json")
            if item["state"] != "checked" or item["date"] != day:
                raise ValueError("jsf_day_invalid")
            expected = day.replace("-", " / ")
            html = sources.pin(root, item["html"]["path"], item["html"]["sha256"])
            csv_path = sources.pin(root, item["csv"]["path"], item["csv"]["sha256"])
            parser = ApplicationDate()
            parser.feed(html.read_text(encoding="utf-8"))
            if parser.values != [expected] or item["requested_date"] != expected:
                raise ValueError("jsf_application_date_evidence_mismatch")
            usable = sources.timed(item)
            source_id = f"jsf:{day}:{item['csv']['sha256']}"
            reader = csv.DictReader(io.StringIO(csv_path.read_bytes().decode("cp932")))
            if reader.fieldnames != JSF_COLUMNS:
                raise ValueError("jsf_csv_columns_changed")
            rows, keys = [], set()
            for number, row in enumerate(reader, 1):
                if None in row or any(v is None for v in row.values()):
                    raise ValueError("jsf_field_count_mismatch")
                key = (row["銘柄コード"], row["市場区分"])
                if key in keys or not re.fullmatch(CODE, key[0]):
                    raise ValueError("jsf_duplicate_or_invalid_code")
                keys.add(key)
                state, amount = parse_reverse_fee(row["品貸料率（円）"])
                counts[state] += 1
                rows.append(
                    {
                        **row,
                        "application_date": day,
                        "source_id": source_id,
                        "source_row": number,
                        "fee_state": state,
                        "total_fee_per_share": str(amount) if amount is not None else None,
                    }
                )
            if len(rows) != item["rows"]:
                raise ValueError("jsf_row_count_mismatch")
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
            manifests.append({**item, "source_id": source_id, "source_available_at": usable})
            if len(manifests) % 150 == 0:
                print(
                    json.dumps({"prepared_jsf_days": len(manifests), "rows": sum(counts.values())}),
                    flush=True,
                )
    if not manifests:
        raise ValueError("jsf_no_dates_in_requested_range")
    return manifests, dict(counts)


def prepare(*, research, public, history, jsf, fee_policy, output, start, end):
    if start > end:
        raise ValueError("invalid_date_range")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = Sources()
    try:
        policy = sources.json(Path(fee_policy).parent, Path(fee_policy).name)
        if policy["policy"] != JSF_POLICY or policy["url"] != JSF_POLICY_URL:
            raise ValueError("unsupported_fee_policy_evidence")
        sources.pin(Path(fee_policy).parent, policy["raw_path"], policy["sha256"])
        sources.timed(policy)
        rights, lots, monthly, missing, unparsed = monthly_observations(
            research, public, sources, start, end
        )
        print(json.dumps({"rights": len(rights), "lot_changes": len(lots)}), flush=True)
        grouped = archive_files(history, sources)
        verify_jsf_calendar(history, sources.json(jsf, "plan.json"), grouped, sources)
        matches = match_rights(rights, grouped, history, sources, start, end)
        for name, rows in (
            ("rights.json", rights),
            ("lot-changes.json", lots),
            ("rights-matches.json", matches),
        ):
            write_new(output / name, rows)
        disclosures = dividend_observations(
            grouped, history, output / "dividend-disclosures.parquet", start, end
        )
        jsf_manifest, fee_counts = jsf_observations(
            jsf, sources, output / "reverse-fees.parquet", start, end
        )
        write_new(
            output / "lineage.json",
            {
                "monthly": monthly,
                "bulk": grouped,
                "jsf": jsf_manifest,
                "fee_policy": policy,
            },
        )
        # Detect mutation during a run, including pinned manifests and extracted text.
        sources.recheck()
        completed = utc_now()
        report = {
            "version": VERSION,
            "state": "prepared_with_gaps",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "prepared_at": completed.isoformat(),
            "available_at": prepared_available_at(completed, sources.times).isoformat(),
            "rights": len(rights),
            "rights_without_stated_ratio": sum(not r["ratio_stated"] for r in rights),
            "rights_types": dict(Counter(r["kind"] for r in rights)),
            "price_events": len(matches),
            "match_states": dict(Counter(r["match_state"] for r in matches)),
            "ordinary_match_states": dict(
                Counter(r["match_state"] for r in matches if r["ordinary_share_candidate"])
            ),
            "lot_change_observations": len(lots),
            "financial_disclosure_rows": disclosures,
            "jsf_days": len(jsf_manifest),
            "jsf_first_date": jsf_manifest[0]["date"],
            "jsf_last_date": jsf_manifest[-1]["date"],
            "reverse_fee_states": fee_counts,
            "missing_monthly_sources": missing,
            "unparsed_ratio_lines": unparsed,
            "gaps": [
                "historical_information_vintages_unverified_acquired_now",
                "pdf_extraction_requires_semantic_review",
                "corporate_action_legal_dates_fractional_cash_and_special_rights_incomplete",
                "dividend_disclosures_are_not_payment_events_no_payment_dates",
                "initial_lots_listing_delisting_and_successors_incomplete",
                "broker_historical_commissions_lending_admin_tax_and_eligibility_terms_missing",
                "jsf_history_shorter_than_ten_years_and_not_broker_inventory",
                "historical_price_limits_ticks_halts_and_execution_rules_incomplete",
                "historical_execution_adapter_not_enabled",
            ],
            "historical_backtest_ready": False,
            "real_execution_enabled": False,
            "sources": list(sources.items.values()),
            "artifacts": [
                {"path": p.name, "sha256": sha_file(p), "bytes": p.stat().st_size}
                for p in sorted(output.iterdir())
                if p.is_file()
            ],
        }
        write_new(output / "manifest.json", report)
        return report
    except Exception as exc:
        write_new(
            output / "failure.json",
            {"state": "failed", "error": str(exc), "historical_backtest_ready": False},
        )
        raise


def verify_bundle(output, decision_at):
    """Allow observation access after preparation; never assert execution readiness."""
    output = Path(output).resolve()
    if (output / "failure.json").exists():
        raise ValueError("preparation_failed")
    report = json.loads((output / "manifest.json").read_bytes())
    if report["version"] not in SUPPORTED_VERSIONS or report["state"] != "prepared_with_gaps":
        raise ValueError("unsupported_bundle_state")
    EvidenceTime(
        timestamp(report["prepared_at"]), recorded_available_at=timestamp(report["available_at"])
    ).require_known(decision_at)
    for item in report["artifacts"]:
        if sha_file(safe_path(output, item["path"])) != item["sha256"]:
            raise ValueError("bundle_artifact_hash_mismatch")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--public", type=Path, default=Path("data/free-market/public"))
    parser.add_argument("--history", type=Path, default=Path("data/jquants/history"))
    parser.add_argument("--jsf", type=Path, default=Path("data/free-market/jsf-history"))
    parser.add_argument("--fee-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    args = vars(parser.parse_args(argv))
    try:
        report = prepare(**args)
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"sources", "artifacts"}},
            ensure_ascii=False,
        )
    )
    # A finished preparation still has unresolved inputs: do not return a ready signal.
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
