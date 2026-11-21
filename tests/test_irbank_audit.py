from trading_system.research.irbank_audit import FIELDS, OFFICIAL, compare
from trading_system.research.irbank_required import decide


def row(loan, stock):
    return dict(zip(FIELDS, (loan, stock, loan - stock), strict=True))


def official(loan, stock):
    return dict(zip(OFFICIAL, map(str, (loan, stock, loan - stock)), strict=True))


def test_alignment_is_measured_without_shifting_actual_decision_dates():
    result = compare(
        {"7203": {"2026-10-28": row(20, 10), "2026-10-29": row(30, 10)}},
        {
            "2026-10-28": {"72030": official(10, 10)},
            "2026-10-29": {"72030": official(20, 10)},
            "2026-10-30": {"72030": official(30, 10)},
        },
    )
    assert result["offset_sessions"]["0"] == {"compared": 2, "matched": 0}
    assert result["offset_sessions"]["1"] == {"compared": 2, "matched": 2}
    assert len(result["mismatch_examples"]) == 2


def test_missing_date_or_security_is_explicit_and_not_a_zero_balance():
    result = compare(
        {"7203": {"2026-10-28": row(0, 0), "2026-10-29": row(0, 0)}},
        {"2026-10-28": {}},
    )
    assert result["by_code"]["7203"]["official_day_missing"] == 1
    assert result["by_code"]["7203"]["official_code_missing"] == 1
    assert result["fields"]["loan_balance"] == {}
    assert decide(None, row(0, 0)) == "no_data"
    assert decide(row(0, 0), row(0, 0)) == "fail"
    assert decide(row(20, 10), row(10, 10)) == "pass"
    assert decide(row(10, 10), row(20, 10)) == "fail"


def test_invalid_official_amount_is_uncompared_not_implicitly_matched():
    result = compare(
        {"7203": {"2026-10-28": row(10, 10)}},
        {"2026-10-28": {"72030": dict(zip(OFFICIAL, ("", "10", "0"), strict=True))}},
    )
    assert result["by_code"]["7203"]["official_values_invalid"] == 1
    assert result["by_code"]["7203"]["uncompared"] == 1


def test_added_evidence_invalidates_unresolved_ticket_but_missing_source_stops(tmp_path):
    import json
    from unittest.mock import Mock

    import pytest

    from trading_system.research.irbank_jsf import Halt
    from trading_system.research.irbank_required import reusable_decision
    from trading_system.research.irbank_universe import sha

    path = tmp_path / "2016-11-18.json"
    path.write_text(
        json.dumps(
            {
                "complete": True,
                "technical_sha256": "technical",
                "audit_sha256": "audit",
                "tested": [{"code": "72030", "state": "no_data", "sources": ["original"]}],
            }
        )
    )
    path.with_suffix(".sha256").write_text(sha(path))
    evidence = Mock()
    evidence.read.return_value = ({}, [], ["original"])
    assert reusable_decision(path, "technical", "audit", evidence)
    evidence.read.return_value = ({}, [], ["original", "new"])
    assert not reusable_decision(path, "technical", "audit", evidence)
    evidence.read.return_value = ({}, [], ["new"])
    with pytest.raises(Halt, match="decision_raw_missing"):
        reusable_decision(path, "technical", "audit", evidence)
    with pytest.raises(Halt, match="decision_audit_changed"):
        reusable_decision(path, "technical", "changed", evidence)
