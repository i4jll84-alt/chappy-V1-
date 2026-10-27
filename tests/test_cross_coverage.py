import hashlib
import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from trading_system import cli
from trading_system.coverage import SOURCES, audit_coverage
from trading_system.market_financial import MarketFinancialStore
from trading_system.reference import ReferenceStore
from trading_system.storage.store import Store

BASE = datetime(2026, 10, 23, tzinfo=timezone.utc)
DECISION = BASE + timedelta(hours=1)
QUERY = {"code": "86970", "from": "2026-10-01", "to": "2026-10-03"}


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("ca")


def price(target="2026-10-01", no_trades=False):
    return {
        "Date": target,
        "Code": "86970",
        "O": 100,
        "H": 110,
        "L": 90,
        "C": 105,
        "Vo": 1000,
        "Va": 100000,
        "AdjFactor": 1,
        "AdjO": 100,
        "AdjH": 110,
        "AdjL": 90,
        "AdjC": 105,
        "AdjVo": 1000,
    } | (
        dict.fromkeys(("O", "H", "L", "C", "Vo", "Va", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo"))
        if no_trades
        else {}
    )


def seed(root, dataset, rows=None, *, offset=0, params=None, failure=None):
    if dataset == "prices":
        store = Store(root)
        default = [price(), price("2026-10-02")]
        query = QUERY
    elif dataset == "calendar":
        store = ReferenceStore(root, "calendar")
        default = [
            {"Date": "2026-10-01", "HolDiv": "1"},
            {"Date": "2026-10-02", "HolDiv": "2"},
            {"Date": "2026-10-03", "HolDiv": "3"},
        ]
        query = {key: value for key, value in QUERY.items() if key != "code"}
    else:
        store = MarketFinancialStore(root, "topix")
        default = [
            {"Date": target, "O": 100, "H": 110, "L": 90, "C": 105}
            for target in ("2026-10-01", "2026-10-02")
        ]
        query = {key: value for key, value in QUERY.items() if key != "code"}
    with store:
        job = store.start(params or query, "standard", refresh=True)["job_id"]
        if failure:
            store.fail(job, failure)
        else:
            body = {"data": default if rows is None else rows}
            store.save_page(job, body, json.dumps(body).encode(), BASE)
            store.finish(job)
        moment = BASE + timedelta(minutes=offset)
        store.db.execute(
            "UPDATE jobs SET started_at=?,updated_at=? WHERE job_id=?", [moment, moment, job]
        )
        if dataset != "prices" and not failure:
            store.db.execute("UPDATE jobs SET completed_at=? WHERE job_id=?", [moment, job])
        if dataset == "topix":
            store.db.execute(
                "UPDATE topix_versions SET available_at=?", [BASE + timedelta(seconds=1)]
            )
    return job


def all_sources(root):
    return {dataset: seed(root, dataset) for dataset in SOURCES}


def audit(root, **kwargs):
    return audit_coverage(
        root, "8697", QUERY["from"], QUERY["to"], kwargs.pop("decision_at", DECISION), **kwargs
    )


def rules(result):
    return {finding["rule"] for finding in result["findings"]}


def hashes(root):
    return {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative, _, _ in SOURCES.values()
        if (root / relative).exists()
    }


def test_clear_snapshot_readonly_and_receipts_are_immutable(tmp_path):
    ids = all_sources(tmp_path)
    before = hashes(tmp_path)
    first, second = audit(tmp_path), audit(tmp_path)
    assert first["state"] == second["state"] == "clear"
    assert first["query"] == QUERY
    assert first["sources"]["prices"]["job_id"] == ids["prices"]
    assert first["sources"]["prices"]["finalization_field"] == "updated_at"
    assert first["days"][1]["tse_business_day"] is True  # HolDiv=2
    assert first["days"][2]["tse_business_day"] is False  # HolDiv=3 is a TSE closure.
    assert first["receipt_path"] != second["receipt_path"]
    assert json.loads(open(first["receipt_path"], encoding="utf-8").read()) == first
    assert hashes(tmp_path) == before


def test_absent_calendar_is_not_created_or_reported_as_price_gap(tmp_path):
    seed(tmp_path, "prices")
    seed(tmp_path, "topix")
    result = audit(tmp_path)
    assert result["state"] == "blocked"
    assert result["sources"]["calendar"]["reason"] == "catalog_missing"
    assert not (tmp_path / "reference").exists()
    assert "prices_coverage_gap" not in rules(result)
    assert all(day["tse_business_day"] is None for day in result["days"])


def test_no_trade_fullnull_is_present_but_missing_open_date_is_gap(tmp_path):
    all_sources(tmp_path)
    seed(tmp_path, "prices", [price(no_trades=True)], offset=1)
    result = audit(tmp_path)
    assert result["state"] == "coverage_failed"
    assert result["days"][0]["prices"] == "no_trades"
    assert result["findings"] == [
        {"rule": "prices_coverage_gap", "dataset": "prices", "date": "2026-10-02"}
    ]


@pytest.mark.parametrize("dataset", ["prices", "topix"])
def test_rows_on_closed_dates_are_flagged(tmp_path, dataset):
    all_sources(tmp_path)
    rows = (
        [price("2026-10-03")]
        if dataset == "prices"
        else [{"Date": "2026-10-03", "O": 100, "H": 110, "L": 90, "C": 105}]
    )
    seed(tmp_path, dataset, rows, offset=1)
    result = audit(tmp_path)
    assert any(
        item == {"rule": "row_on_closed_day", "dataset": dataset, "date": "2026-10-03"}
        for item in result["findings"]
    )


@pytest.mark.parametrize("dataset", list(SOURCES))
def test_latest_failed_attempt_blocks_clean_fallback_and_hides_errors(tmp_path, dataset):
    all_sources(tmp_path)
    job = seed(tmp_path, dataset, failure="SECRET-NOT-FOR-OUTPUT", offset=1)
    result = audit(tmp_path)
    assert result["state"] == "blocked"
    assert result["sources"][dataset]["job_id"] == job
    assert result["sources"][dataset]["status"] == "incomplete"
    assert "SECRET-NOT-FOR-OUTPUT" not in json.dumps(result)


def test_exact_query_only_does_not_stitch_or_use_other_code(tmp_path):
    all_sources(tmp_path)
    seed(tmp_path, "prices", [price()], offset=1, params=QUERY | {"to": "2026-10-01"})
    result = audit_coverage(tmp_path, "8697", "2026-09-30", "2026-10-03", DECISION)
    assert result["state"] == "blocked"
    assert result["sources"]["prices"]["reason"] == "no_exact_query_job_at_cutoff"
    assert result["sources"]["prices"]["job_id"] is None
    other = audit_coverage(tmp_path, "7203", QUERY["from"], QUERY["to"], DECISION)
    assert other["sources"]["prices"]["job_id"] is None


def test_history_status_not_reconstructed_before_current_update(tmp_path):
    all_sources(tmp_path)
    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as db:
        db.execute("UPDATE jobs SET updated_at=?", [DECISION + timedelta(seconds=1)])
    result = audit(tmp_path)
    assert result["sources"]["prices"]["status"] == "indeterminate_at_cutoff"
    assert "prices_coverage_gap" not in rules(result)


def test_version_availability_blocks_snapshot_without_current_view_fallback(tmp_path):
    all_sources(tmp_path)
    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as db:
        db.execute("UPDATE price_versions SET available_at=?", [DECISION + timedelta(seconds=1)])
    result = audit(tmp_path)
    assert result["sources"]["prices"]["reason"] == "version_unavailable_at_cutoff"
    assert "prices_coverage_gap" not in rules(result)


def test_dangling_version_reference_is_not_a_missing_date_or_duplicate(tmp_path):
    all_sources(tmp_path)
    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as db:
        db.execute("DELETE FROM price_versions WHERE target_date='2026-10-02'")
    result = audit(tmp_path)
    assert result["sources"]["prices"]["reason"] == "snapshot_integrity_mismatch"
    assert result["sources"]["prices"]["dangling_version_references"] == 1
    assert result["sources"]["prices"]["duplicate_observations"] == 0
    assert result["state"] == "blocked"
    assert "prices_coverage_gap" not in rules(result)


@pytest.mark.parametrize("dataset", list(SOURCES))
def test_completion_delay_boundary_for_all_datasets(tmp_path, dataset):
    all_sources(tmp_path)
    seed(tmp_path, dataset, offset=1)
    finalized = BASE + timedelta(minutes=1)
    before = audit(tmp_path, decision_at=finalized + timedelta(seconds=0.5))
    assert before["sources"][dataset]["status"] == "not_yet_available"
    assert before["sources"][dataset]["reason"] == "completion_delay"
    boundary = audit(tmp_path, decision_at=finalized + timedelta(seconds=1))
    assert boundary["state"] == "clear"


@pytest.mark.parametrize("dataset", ["prices", "topix"])
def test_unrelated_query_correction_does_not_leak_current_versions(tmp_path, dataset):
    all_sources(tmp_path)
    original = audit(tmp_path)["sources"][dataset]
    changed = price() | {"C": 106, "AdjC": 106}
    if dataset == "topix":
        changed = {key: changed[key] for key in ("Date", "O", "H", "L", "C")}
    narrowed = {"from": QUERY["from"], "to": QUERY["from"]}
    if dataset == "prices":
        narrowed["code"] = QUERY["code"]
    seed(tmp_path, dataset, [changed], offset=1, params=narrowed)
    with duckdb.connect(str(tmp_path / SOURCES[dataset][0]), read_only=True) as db:
        current = db.execute(
            f"SELECT content_hash FROM {dataset[:-1] if dataset == 'prices' else dataset}_versions "
            "WHERE target_date='2026-10-01' ORDER BY version_id DESC LIMIT 1"
        ).fetchone()[0]
    assert current != original["versions"][0]["content_hash"]
    result = audit(tmp_path)
    assert result["state"] == "clear"
    assert result["sources"][dataset]["job_id"] == original["job_id"]
    assert result["sources"][dataset]["versions"] == original["versions"]


@pytest.mark.parametrize("dataset", ["prices", "topix"])
def test_latest_quality_failed_snapshot_blocks_gap_derivation(tmp_path, dataset):
    all_sources(tmp_path)
    broken = price() | {"H": 80}
    if dataset == "topix":
        broken = {key: broken[key] for key in ("Date", "O", "H", "L", "C")}
    job = seed(tmp_path, dataset, [broken], offset=1)
    result = audit(tmp_path)
    assert result["sources"][dataset]["job_id"] == job
    assert result["sources"][dataset]["status"] == "quality_failed"
    assert result["state"] == "blocked"
    assert dataset + "_coverage_gap" not in rules(result)


def test_unreadable_catalog_does_not_print_its_contents(tmp_path):
    tmp_path.joinpath("catalog.duckdb").write_text("SECRET-NOT-FOR-OUTPUT")
    result = audit(tmp_path)
    assert result["sources"]["prices"]["reason"] == "catalog_unreadable_or_incompatible"
    assert "SECRET-NOT-FOR-OUTPUT" not in json.dumps(result)
    assert tmp_path.joinpath("catalog.duckdb").read_text() == "SECRET-NOT-FOR-OUTPUT"


def test_bad_latest_calendar_does_not_use_older_expected_days(tmp_path):
    all_sources(tmp_path)
    seed(tmp_path, "calendar", [{"Date": "2026-10-01", "HolDiv": "1"}], offset=1)
    result = audit(tmp_path)
    assert result["sources"]["calendar"]["status"] == "quality_failed"
    assert all(day["tse_business_day"] is None for day in result["days"])


def test_empty_successful_price_query_reports_coverage_gaps(tmp_path):
    all_sources(tmp_path)
    seed(tmp_path, "prices", [], offset=1)
    result = audit(tmp_path)
    assert result["sources"]["prices"]["status"] == "empty"
    assert result["state"] == "coverage_failed"
    assert sum(item["rule"] == "prices_coverage_gap" for item in result["findings"]) == 2


def test_cli_audit_needs_no_secret_or_network_and_returns_clear(tmp_path, monkeypatch, capsys):
    all_sources(tmp_path)
    monkeypatch.setattr(cli, "api_key", lambda: pytest.fail("Audit requested credentials"))
    monkeypatch.setattr(cli, "JQuantsClient", lambda *_a, **_k: pytest.fail("Audit requested API"))
    assert (
        cli.main(
            [
                "--data-root",
                str(tmp_path),
                "audit-coverage",
                "--code",
                "8697",
                "--from",
                QUERY["from"],
                "--to",
                QUERY["to"],
                "--decision-at",
                DECISION.isoformat(),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "clear"


def test_cli_missing_sources_and_invalid_timestamp_exit_codes(tmp_path, capsys):
    args = [
        "--data-root",
        str(tmp_path / "missing"),
        "audit-coverage",
        "--code",
        "8697",
        "--from",
        QUERY["from"],
        "--to",
        QUERY["to"],
    ]
    assert cli.main(args) == 3
    assert json.loads(capsys.readouterr().out)["state"] == "blocked"
    assert cli.main(args + ["--decision-at", "2026-10-23T01:00:00"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ValueError"
