import hashlib
import json
from datetime import datetime

import pytest

from trading_system import plan_forward, plan_retirement
from trading_system.plan_retirement import OLD_SPEC, retire_previous_c

NEW = {
    **OLD_SPEC,
    "adapter": "plan_c_pead_forward",
    "version": plan_retirement.NEW_VERSION,
    "start": "2026-11-25",
}


def test_replacement_archives_every_file_and_preserves_pending_decisions(tmp_path):
    root = tmp_path / "data/plan-forward"
    old = root / "accounts/C"
    decision = old / "sessions/2026-11-26/decision.json"
    plan_forward.save(old / "enrollment.json", {"spec": OLD_SPEC})
    plan_forward.save(decision, {"buys": [], "target": "2026-11-26"})
    original = decision.read_bytes()
    now = datetime(2026, 11, 26, 3, tzinfo=plan_forward.JST)
    retire_previous_c(root, NEW, now)
    folder = root / "retired/C-c1-pullback-guarded-v1"
    manifest = json.loads((folder / "retirement.json").read_bytes())
    assert (
        manifest["files_sha256"]["sessions/2026-11-26/decision.json"]
        == hashlib.sha256(original).hexdigest()
    )
    assert (folder / "account/sessions/2026-11-26/decision.json").read_bytes() == original
    assert not old.exists()
    retire_previous_c(root, NEW, now)
    plan_forward.save(old / "enrollment.json", {"spec": NEW})
    retire_previous_c(root, NEW, now)
    assert json.loads((old / "enrollment.json").read_bytes())["spec"] == NEW


def test_pead_exit_status_preserves_data_and_valuation_warnings():
    row = dict(adapter="plan_c_pead_forward", decision_status="exits_only")
    assert plan_forward.display_status(dict(row, status="waiting_close")) == "exits_only"
    assert plan_forward.display_status(dict(row, status="waiting_inputs")) == "pead_inputs"
    assert plan_forward.display_status(dict(row, status="needs_review")) == "needs_review"


def test_unknown_book_is_not_retired(tmp_path):
    old = tmp_path / "accounts/C"
    plan_forward.save(old / "enrollment.json", {"spec": {**OLD_SPEC, "capital": 1}})
    with pytest.raises(ValueError, match="account_terms_changed"):
        retire_previous_c(tmp_path, NEW, datetime.now(plan_forward.JST))
    assert old.is_dir() and not (tmp_path / "retired").exists()


def test_pead_morning_audit_validates_the_successor_version_and_market_gate(tmp_path):
    from datetime import timedelta

    from trading_system.plan_morning_check import assess_plan

    start = datetime(2026, 11, 26, 7, 40, tzinfo=plan_forward.JST)
    deadline = start.replace(hour=9, minute=0)
    signal, target = "2026-11-25", "2026-11-26"
    spec = {**NEW, "stage": "active"}
    path = tmp_path / "data/plan-forward/accounts/C/sessions" / target / "decision.json"
    doc = {
        "version": NEW["version"],
        "strategy": "GRID_DIP_H60_ATR40",
        "signal_date": signal,
        "target": target,
        "state": "ready",
        "recorded_at": start.isoformat(),
        "cutoff": (start - timedelta(seconds=2)).isoformat(),
        "picks": [],
        "plan": {"buys": [], "sells": []},
        "allow_entries": False,
    }

    def check(record):
        plan_forward.save(path, record)
        return assess_plan(
            tmp_path, "C", spec, signal, target, start, deadline, start + timedelta(minutes=1)
        )

    assert check(doc)["state"] == "guarded"
    assert check(doc | {"version": OLD_SPEC["version"]})["state"] == "unverified"
    assert check(doc | {"plan": {"buys": [{"code": "12340"}]}})["state"] == "unverified"
    assert check(doc | {"recorded_at": deadline.isoformat()})["state"] == "unverified"
