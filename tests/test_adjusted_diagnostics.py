import copy
import json
from fractions import Fraction

import pytest
from test_adjusted_history import row

from trading_system.adjusted_diagnostics import analyze_rows, diagnose, rounded
from trading_system.adjusted_history import ADJUSTED, DAILY, RAW
from trading_system.history_download import Archive, identity, sha_file
from trading_system.storage.store import canonical


def api_row(day="2026-10-30", price="100", factor="1", event="", code="01230"):
    result = row(day, price, factor, event, code)
    result.update({field: price for field in ADJUSTED[:4]})
    result["AdjVo"] = "1000"
    return result


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.25", "1.3"), ("1.2499999999999999", "1.2"), ("0.05", "0.1")],
)
def test_reference_rounding_keeps_exact_boundary(value, expected):
    assert rounded(Fraction(value)) == Fraction(expected)


def test_direct_event_products_exclude_same_day_and_rights_volume():
    latest = api_row("2026-10-30", "100", "0.8", "3")
    middle = api_row("2026-10-29", "100", "10", "2")
    oldest = api_row("2026-10-28", "10")
    for item in (middle, oldest):
        item.update({field: "80.0" for field in ADJUSTED[:4]})
    oldest["AdjVo"] = "100"
    result = analyze_rows([latest, middle, oldest], "01230", "2026-10-28", "2026-10-30")
    assert result["state"] == "checked" and result["rows"] == 3


def test_near_midpoint_is_reported_as_difference_without_tolerance():
    latest = api_row("2026-10-30", factor="0.3333333333333333", event="1")
    oldest = api_row("2026-10-29", "3.75")
    oldest.update({field: "1.3" for field in ADJUSTED[:4]})
    oldest["AdjVo"] = "3000.0"
    before = copy.deepcopy([latest, oldest])
    result = analyze_rows([latest, oldest], "01230", "2026-10-29", "2026-10-30")
    assert result["difference_count"] == 4
    for difference in result["differences"]:
        assert difference["exact_decimal_calculation"] == "1.2"
        assert difference["one_tenth_difference"]
        assert Fraction(
            int(difference["midpoint_distance_numerator"]),
            int(difference["midpoint_distance_denominator"]),
        ) == Fraction("0.000000000000000125")
    assert [latest, oldest] == before


def test_nulls_are_kept_and_null_differences_not_called_rounding():
    source = api_row()
    for field in (*RAW, *ADJUSTED):
        source[field] = None
    assert analyze_rows([source], "01230", "2026-10-30", "2026-10-30")["null_values"] == 5
    source["AdjC"] = "0"
    result = analyze_rows([source], "01230", "2026-10-30", "2026-10-30")
    assert result["differences"][0]["kind"] == "null_mismatch"


@pytest.mark.parametrize("problem", ["duplicate", "partial", "missing_key"])
def test_invalid_evidence_is_not_silently_normalized(problem):
    source = api_row()
    rows = [source]
    if problem == "duplicate":
        rows.append(dict(source))
    elif problem == "partial":
        source["C"] = None
    else:
        del source["AdjC"]
    with pytest.raises((ValueError, KeyError)):
        analyze_rows(rows, "01230", "2026-10-30", "2026-10-30")


def saved_archive(root):
    archive = Archive(root)
    results = []
    for code in ("01230", "130A0"):
        record = api_row(code=code)
        if code == "130A0":
            record["AdjC"] = "100.1"
        params = {"code": code, "from": "2026-10-30", "to": "2026-10-30"}
        job_id = identity([DAILY, params])
        with archive.db:
            archive.db.execute(
                "INSERT INTO api_jobs(id,endpoint,params_json,state) VALUES(?,?,?,'pending')",
                (job_id, DAILY, canonical(params)),
            )

        class SavedPage:
            def get(self, endpoint, query):
                payload = {"data": [record]}
                return payload, canonical(payload).encode()

        job = archive.db.execute("SELECT * FROM api_jobs WHERE id=?", (job_id,)).fetchone()
        archive.fetch_json_page(SavedPage(), job)
        results.append({"code": code, "job_id": job_id, "api_rows": 1})
    raw_path = archive.db.execute("SELECT raw_path FROM api_pages LIMIT 1").fetchone()[0]
    archive.close()
    comparison = {
        "state": "issues_found",
        "from": "2026-10-30",
        "to": "2026-10-30",
        "completed_codes": 2,
        "total_codes": 2,
        "selection": [{"code": r["code"]} for r in results],
        "results": results,
    }
    (root / "comparison.json").write_text(canonical(comparison), encoding="utf-8")
    return root / raw_path


def test_diagnosis_includes_matches_and_mismatches_and_preserves_sources(tmp_path):
    root = tmp_path / "r"
    raw = saved_archive(root)
    before = sha_file(raw)
    output = tmp_path / "report.json"
    result = diagnose(root, output)
    assert result["summary"]["codes"] == 2 and result["summary"]["matched_codes"] == 1
    assert result["summary"]["differences"] == 1
    assert result["summary"]["max_midpoint_distance"] == "0.05"
    assert result["historical_backtest_ready"] is False
    assert result["provider_algorithm_verified"] is False
    assert sha_file(raw) == before
    with pytest.raises(ValueError, match="new_report_outside_evidence_required"):
        diagnose(root, output)


def test_tampered_raw_is_rejected_without_report(tmp_path):
    root = tmp_path / "r"
    raw = saved_archive(root)
    raw.write_bytes(b"corrupt")
    output = tmp_path / "report.json"
    with pytest.raises(ValueError, match="api_raw_changed"):
        diagnose(root, output)
    assert not output.exists()


def test_incomplete_selection_is_not_treated_as_complete(tmp_path):
    root = tmp_path / "r"
    saved_archive(root)
    path = root / "comparison.json"
    comparison = json.loads(path.read_text())
    comparison["completed_codes"] = 1
    path.write_text(canonical(comparison), encoding="utf-8")
    with pytest.raises(ValueError, match="complete_comparison_required"):
        diagnose(root, tmp_path / "report.json")
