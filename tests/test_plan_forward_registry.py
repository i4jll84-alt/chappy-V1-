import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from trading_system import plan_forward as plans


def setup_catalog(tmp_path, *rows):
    path = tmp_path / "config/forward-plans.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"plans": rows}), encoding="utf-8")


def active(key="A"):
    return dict(
        id=key,
        name=key + " <paper>",
        stage="active",
        adapter="sample",
        version="v1",
        start="2026-11-19",
        capital=3_000_000,
    )


def test_future_plan_can_be_researched_then_enabled_without_moving_an_account(
    tmp_path, monkeypatch
):
    calls = []

    def advance(_project, book, *, start, capital, now):
        calls.append(book)
        return dict(
            status="ready",
            initial_capital=capital,
            cash=capital,
            nav=capital,
            positions={},
            last_mark=start,
        )

    monkeypatch.setattr(
        plans.importlib, "import_module", lambda name: SimpleNamespace(advance=advance)
    )
    setup_catalog(tmp_path, active(), dict(id="C", name="test", stage="research"))
    now = datetime(2026, 11, 19, 8, tzinfo=plans.JST)
    one = plans.run_due(tmp_path, now=now)
    assert list(one["plans"]) == ["A"]
    root = tmp_path / "data/plan-forward"
    a = root / "accounts/A/enrollment.json"
    original = a.read_bytes()
    assert not (root / "accounts/C").exists()
    setup_catalog(tmp_path, active(), active("C"))
    two = plans.run_due(tmp_path, now=now)
    assert set(two["plans"]) == {"A", "C"}
    assert calls == [a.parent, a.parent, root / "accounts/C"]
    assert original == a.read_bytes()
    assert "&lt;paper&gt;" in (root / "summary.html").read_text(encoding="utf-8")


def test_terms_and_partial_failure_do_not_reset_books_or_suppress_other_plan(tmp_path, monkeypatch):
    def execute(_p, book, *, start, capital, now):
        if book.name == "B":
            raise RuntimeError("sensitive details")
        return dict(status="waiting_for_first_close", initial_capital=capital)

    monkeypatch.setattr(
        plans.importlib, "import_module", lambda _: SimpleNamespace(advance=execute)
    )
    setup_catalog(tmp_path, active(), active("B"), active("C"))
    result = plans.run_due(tmp_path)
    assert (
        result["status"] == "error" and result["plans"]["C"]["status"] == "waiting_for_first_close"
    )
    assert result["plans"]["B"]["issue"] == "RuntimeError"
    setup_catalog(tmp_path, active() | {"capital": 4_000_000}, active("C"))
    result = plans.run_due(tmp_path)
    assert result["plans"]["A"]["issue"] == "account_terms_changed"
    assert (
        plans.read(tmp_path / "data/plan-forward/accounts/A/enrollment.json")["spec"]["capital"]
        == 3_000_000
    )


def test_catalog_rejects_duplicate_or_unsafe_account_paths(tmp_path):
    setup_catalog(tmp_path, active(), active())
    with pytest.raises(ValueError):
        plans.catalog(tmp_path)
    setup_catalog(tmp_path, active("../B"))
    with pytest.raises(ValueError):
        plans.catalog(tmp_path)


def test_brief_status_reads_all_saved_plans_and_preserves_warnings(tmp_path, capsys):
    path = tmp_path / "data/plan-forward/summary.json"
    path.parent.mkdir(parents=True)
    recovery = {"state": "error", "error_type": "TimeoutError"}
    report = {
        "status": "error",
        "checked_at": "2026-11-27T08:55:00+09:00",
        "plans": {
            "A": {"status": "waiting_inputs", "issue": "missing", "positions": {"test": 100}},
            "B": {"status": "blocked", "morning_recovery": recovery, "decision_cutoff": None},
            "C": {
                "status": "needs_review",
                "coverage_ok": False,
                "valuation_complete": False,
                "entitlement_review": [{"code": "test"}],
                "last_review": {"date": "2026-11-26", "issues": [["test", "missing"]]},
            },
            "A9": {"status": "waiting_signal", "decision_recorded_at": None},
            "A10": {"status": "waiting_signal"},
        },
    }
    saved = json.dumps(report).encode()
    path.write_bytes(saved)
    args = ["--project", str(tmp_path)]
    assert plans.main([*args, "--status-brief"]) == 2
    brief = json.loads(capsys.readouterr().out)
    assert brief["status"] == "error" and brief["checked_at"] == report["checked_at"]
    assert brief["plans"].keys() == report["plans"].keys()
    assert brief["plans"]["A"] == {"status": "waiting_inputs", "issue": "missing"}
    assert brief["plans"]["B"] == report["plans"]["B"]
    assert brief["plans"]["C"] == {
        "status": "needs_review",
        "coverage_ok": False,
        "valuation_complete": False,
        "entitlement_review_count": 1,
        "last_review": {"date": "2026-11-26", "issue_count": 1},
    }
    assert brief["plans"]["A9"] == report["plans"]["A9"]
    assert brief["plans"]["A10"] == report["plans"]["A10"]
    assert plans.main([*args, "--status"]) == 2
    assert json.loads(capsys.readouterr().out) == report
    assert path.read_bytes() == saved


def test_brief_status_before_first_run_creates_no_data(tmp_path, capsys):
    assert plans.main(["--project", str(tmp_path), "--status-brief"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "not_started", "plans": {}}
    assert list(tmp_path.iterdir()) == []
