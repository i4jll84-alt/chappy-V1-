import csv
import hashlib
import io
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading_system.backtest.historical_evidence import (
    JSF_POLICY,
    JSF_POLICY_URL,
    EvidenceTime,
    ReverseFeeObservation,
    SplitRatio,
    ordinary_share_candidate,
    parse_reverse_fee,
    prepared_available_at,
)
from trading_system.historical_inputs import (
    DIVIDEND_COLUMNS,
    JSF_COLUMNS,
    Sources,
    archive_files,
    parse_right,
    prepare,
    verify_bundle,
)

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
META = {"observed_at": NOW.isoformat(), "available_at": (NOW + timedelta(seconds=1)).isoformat()}


def test_historical_publication_does_not_backdate_availability():
    time = EvidenceTime(NOW, datetime(2016, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="not_available"):
        time.require_known(NOW)
    time.require_known(NOW + timedelta(seconds=1))


def test_timezone_and_preparation_boundaries():
    with pytest.raises(ValueError, match="Timezone"):
        EvidenceTime(NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="available_before"):
        EvidenceTime(NOW, recorded_available_at=NOW)
    late = EvidenceTime(NOW, recorded_available_at=NOW + timedelta(hours=1))
    assert prepared_available_at(NOW, [late]) == NOW + timedelta(hours=1)
    assert prepared_available_at(NOW + timedelta(hours=2), [late]) == NOW + timedelta(
        hours=2, seconds=1
    )
    with pytest.raises(ValueError, match="source_evidence"):
        prepared_available_at(NOW, [])


@pytest.mark.parametrize(
    "raw,state,amount",
    [
        ("0.00", "numeric", Decimal("0.00")),
        ("0.15", "numeric", Decimal("0.15")),
        ("*****", "not_incurred", None),
        ("＊＊＊＊＊", "not_incurred", None),
        ("", "missing", None),
        ("-", "missing", None),
        ("－", "missing", None),
    ],
)
def test_reverse_fee_states(raw, state, amount):
    assert parse_reverse_fee(raw) == (state, amount)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-0.1", "unknown", "***"])
def test_invalid_fee_is_not_zero(raw):
    with pytest.raises(ValueError, match="invalid_reverse_fee"):
        parse_reverse_fee(raw)


def fee(raw):
    return ReverseFeeObservation(date(2026, 10, 30), raw, EvidenceTime(NOW), EvidenceTime(NOW))


def test_published_total_is_not_multiplied_by_calendar_days():
    # A 3-day total of 0.15 gives 15 yen for 100 assessed shares, not 45 yen.
    assert fee("0.15").amount(
        assessed_shares=100, accounting_at=NOW + timedelta(seconds=1)
    ) == Decimal(15)
    assert fee("*****").amount(assessed_shares=100, accounting_at=NOW + timedelta(seconds=1)) == 0
    with pytest.raises(ValueError, match="missing"):
        fee("").amount(assessed_shares=0, accounting_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="not_available"):
        fee("0.15").amount(assessed_shares=100, accounting_at=NOW)


@pytest.mark.parametrize("quantity", [-1, 1.5, True, "100"])
def test_assessed_quantity_must_be_explicit_integer(quantity):
    with pytest.raises(ValueError, match="integer_assessed"):
        fee("0.15").amount(assessed_shares=quantity, accounting_at=NOW + timedelta(seconds=1))


def test_fractional_split_is_exact_and_special_rights_not_inferred():
    row = parse_right(
        "1234 A 2020.01.02 2020.01.03 1:3 株式分割", 1, "source", META["available_at"]
    )
    assert row["price_multiplier"] == "1/3"
    assert row["quantity_multiplier"] == "3/1"
    assert row["legal_effective_date"] is None and row["payment_date"] is None
    assert not row["usable_as_execution_action"]
    for ratio in ("1:1.05", "1:0.05"):
        special = parse_right(
            f"1234 A 2020.01.02 2020.01.03 {ratio} 株式無償割当て", 1, "x", META["available_at"]
        )
        assert special["price_multiplier"] is None
        assert special["kind"] == "unsupported_allocation_or_product"
    assert SplitRatio("consolidation", Fraction(5), Fraction(1)).price_multiplier == 5
    with pytest.raises(ValueError, match="direction"):
        SplitRatio("split", Fraction(5), Fraction(1))


@pytest.mark.parametrize(
    "code,product,expected",
    [
        ("12340", "011", True),
        ("12345", "011", False),
        ("12340", "013", False),
        ("12340", "012", False),
        ("12340", None, False),
    ],
)
def test_ordinary_classification_does_not_accept_preferred_or_funds(code, product, expected):
    assert ordinary_share_candidate(code, product) is expected


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def fixture_archive(tmp_path):
    research, public, history, jsf = [
        tmp_path / x for x in ("research", "public", "history", "jsf")
    ]
    for path in (research, public, history, jsf):
        path.mkdir()
    manifest = []
    for kind, prefix, text in [
        ("kenri", "rights-text-v2-", "1234 Test 2020.01.02 2020.01.03 1:3 株式分割"),
        ("idou", "changes-text-", "2020.01.01 1234 Test 100株 (1000株)"),
        ("kabusu", None, ""),
    ]:
        pdf = public / (kind + ".pdf")
        pdf.write_bytes(b"test pinned PDF fixture " + kind.encode())
        item = {
            "kind": kind,
            "month": "2020-01",
            "raw_path": pdf.name,
            "sha256": digest(pdf),
            "url": "https://www.jpx.co.jp/" + pdf.name,
            **META,
        }
        manifest.append(item)
        if prefix:
            dump(
                research / (prefix + "2020-01.json"),
                {"source": item, "pages": [{"page": 1, "text": text}]},
            )
    dump(research / "monthly-coverage-v2.json", {"manifest": manifest})
    tables = {
        "/equities/bars/daily": [
            {"Date": "2020-01-02", "Code": "12340", "ExRT": "1", "AdjFactor": "0.3333333"}
        ],
        "/equities/master": [{"Date": "2020-01-02", "Code": "12340", "ProdCat": "011"}],
        "/fins/summary": [
            {
                **dict.fromkeys(DIVIDEND_COLUMNS, ""),
                "DiscDate": "2020-01-02",
                "DiscTime": "15:00",
                "Code": "12340",
                "DivFY": "10",
                "FDivFY": "12",
            }
        ],
        "/markets/calendar": [{"Date": "2020-01-02", "HolDiv": "1"}],
    }
    with sqlite3.connect(history / "history.sqlite") as db:
        db.execute(
            "CREATE TABLE bulk_files(id,endpoint,object_key,modified,state,raw_path,sha256,"
            "parquet_path,parquet_sha256,observed_at,available_at,rows)"
        )
        for n, (endpoint, rows) in enumerate(tables.items()):
            path = history / f"{n}.parquet"
            pq.write_table(pa.Table.from_pylist(rows), path)
            raw = history / f"{n}.csv"
            raw.write_bytes(b"fixture raw " + str(n).encode())
            db.execute(
                "INSERT INTO bulk_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(n),
                    endpoint,
                    str(n),
                    NOW.isoformat(),
                    "complete",
                    raw.name,
                    digest(raw),
                    path.name,
                    digest(path),
                    META["observed_at"],
                    META["available_at"],
                    len(rows),
                ),
            )
    day = "2020-01-02"
    dump(
        jsf / "plan.json",
        {
            "dates": [day],
            "start": day,
            "end": day,
            "calendar_sha256": digest(history / "3.parquet"),
        },
    )
    dump(jsf / "status.json", {"state": "checked"})
    html = jsf / "day.html"
    html.write_text('<input name="mkYmd" value="2020 / 01 / 02">', encoding="utf-8")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=JSF_COLUMNS)
    writer.writeheader()
    for i, value in enumerate(["0.15", "*****", "", "0.00"]):
        writer.writerow(
            {
                **dict.fromkeys(JSF_COLUMNS, ""),
                "銘柄コード": f"123{i}",
                "市場区分": "東証",
                "品貸料率（円）": value,
            }
        )
    raw = jsf / "day.csv"
    raw.write_bytes(buf.getvalue().encode("cp932"))
    dump(
        jsf / "days" / (day + ".json"),
        {
            "state": "checked",
            "date": day,
            "requested_date": "2020 / 01 / 02",
            "rows": 4,
            "csv": {"path": raw.name, "sha256": digest(raw)},
            "html": {"path": html.name, "sha256": digest(html)},
            **META,
        },
    )
    policy_raw = tmp_path / "policy.html"
    policy_raw.write_bytes(b"official semantics fixture")
    policy = tmp_path / "policy.json"
    dump(
        policy,
        {
            "policy": JSF_POLICY,
            "url": JSF_POLICY_URL,
            "raw_path": policy_raw.name,
            "sha256": digest(policy_raw),
            **META,
        },
    )
    return {
        "research": research,
        "public": public,
        "history": history,
        "jsf": jsf,
        "fee_policy": policy,
        "output": tmp_path / "prepared",
        "start": date(2020, 1, 2),
        "end": date(2020, 1, 2),
    }


def test_full_preparation_keeps_gaps_and_original_states(fixture_archive):
    report = prepare(**fixture_archive)
    assert report["state"] == "prepared_with_gaps" and not report["historical_backtest_ready"]
    assert report["rights"] == 1 and report["lot_change_observations"] == 1
    assert report["match_states"] == {"code_date_type_direction_match": 1}
    assert report["reverse_fee_states"] == {"numeric": 2, "not_incurred": 1, "missing": 1}
    out = fixture_archive["output"]
    rows = pq.read_table(out / "reverse-fees.parquet").to_pylist()
    assert rows[1]["品貸料率（円）"] == "*****" and rows[1]["total_fee_per_share"] is None
    dividend = pq.read_table(out / "dividend-disclosures.parquet").to_pylist()[0]
    assert (dividend["DivFY"], dividend["FDivFY"], dividend["payment_date"]) == ("10", "12", None)
    at = datetime.fromisoformat(report["available_at"])
    assert verify_bundle(out, at)["version"] == report["version"]
    with pytest.raises(ValueError, match="not_available"):
        verify_bundle(out, at - timedelta(microseconds=1))
    with pytest.raises(FileExistsError):
        prepare(**fixture_archive)
    (out / "rights.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="hash_mismatch"):
        verify_bundle(out, at)


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("raw", "hash_mismatch"),
        ("html", "application_date"),
        ("duplicate", "duplicate_corporate"),
        ("latest", "latest_bulk"),
        ("calendar", "calendar_sessions"),
    ],
)
def test_source_errors_leave_failure_not_complete(fixture_archive, mutation, error):
    args = fixture_archive
    if mutation == "raw":
        (args["public"] / "kenri.pdf").write_bytes(b"changed")
    elif mutation == "html":
        path = args["jsf"] / "day.html"
        path.write_text('<input name="mkYmd" value="2020 / 01 / 03">', encoding="utf-8")
        manifest = args["jsf"] / "days/2020-01-02.json"
        obj = json.loads(manifest.read_bytes())
        obj["html"]["sha256"] = digest(path)
        dump(manifest, obj)
    elif mutation == "duplicate":
        path = args["research"] / "rights-text-v2-2020-01.json"
        obj = json.loads(path.read_bytes())
        obj["pages"][0]["text"] += "\n" + obj["pages"][0]["text"]
        dump(path, obj)
    elif mutation == "latest":
        with sqlite3.connect(args["history"] / "history.sqlite") as db:
            db.execute(
                "INSERT INTO bulk_files SELECT id,endpoint,object_key,'2030-01-01','pending',"
                "raw_path,sha256,parquet_path,parquet_sha256,observed_at,available_at,rows "
                "FROM bulk_files WHERE endpoint='/equities/master'"
            )
    elif mutation == "calendar":
        path = args["jsf"] / "plan.json"
        obj = json.loads(path.read_bytes())
        obj["dates"] = []
        dump(path, obj)
    with pytest.raises(ValueError, match=error):
        prepare(**args)
    assert (args["output"] / "failure.json").exists()
    assert not (args["output"] / "manifest.json").exists()


def test_pin_path_escape_and_midrun_change(tmp_path):
    with pytest.raises(ValueError, match="outside_root"):
        Sources().pin(tmp_path, "../outside")
    path = tmp_path / "a"
    path.write_bytes(b"one")
    sources = Sources()
    sources.pin(tmp_path, "a")
    path.write_bytes(b"two")
    with pytest.raises(ValueError, match="changed_during"):
        sources.recheck()


def test_no_bulk_sources_is_not_success(tmp_path):
    with sqlite3.connect(tmp_path / "history.sqlite") as db:
        db.execute("CREATE TABLE bulk_files(endpoint,object_key,modified,state)")
    with pytest.raises(ValueError, match="endpoint_missing"):
        archive_files(tmp_path, Sources())
