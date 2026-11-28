import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system import plan_forward
from trading_system.research import plan_a_forward as plan

START = "2026-11-25"
CAPITAL = 3_000_000


def clock(day, hour=18):
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+09:00")


def test_expiry_counts_trading_sessions_across_extended_exchange_holidays(tmp_path, monkeypatch):
    first = date(2026, 12, 15)

    # Include a two-week exchange closure to exercise the wider calendar horizon.
    def calendar(root, start, now, *, future_days=20):
        days = (start + timedelta(days=n) for n in range(future_days + 1))
        return {
            d.isoformat(): "1"
            if d.weekday() < 5 and not date(2026, 12, 23) <= d <= date(2027, 1, 4)
            else "0"
            for d in days
        }, []

    monkeypatch.setattr(plan, "calendar_days", calendar)
    for term, expected in ((9, "2027-01-11"), (10, "2027-01-12")):
        target, due, _ = plan.schedule(tmp_path, str(first), clock(first), term)
        assert (target, due) == ("2026-12-16", expected)

    def incomplete(*args, **kwargs):
        days, refs = calendar(*args, **kwargs)
        del days["2026-12-29"]  # A missing row cannot be treated as an exchange holiday.
        return days, refs

    monkeypatch.setattr(plan, "calendar_days", incomplete)
    with pytest.raises(ValueError, match="incomplete_future_calendar"):
        plan.schedule(tmp_path, str(first), clock(first), 10)


@pytest.fixture
def family(tmp_path, monkeypatch):
    path = tmp_path / "config/forward-plans.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "plans": [
                    dict(
                        id=key,
                        name=key,
                        stage="active",
                        adapter="plan_a_forward",
                        version=version,
                        start=START,
                        capital=CAPITAL,
                    )
                    for key, (version, _) in plan.FIXED_TERMS.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def calendar(root, start, now, *, future_days=20):
        days = (start + timedelta(days=n) for n in range(future_days + 1))
        return {d.isoformat(): "1" if d.weekday() < 5 else "0" for d in days}, []

    def screen(project, signal, target, due, cutoff):
        calls.append(signal)
        return {
            "strategy": plan.STRATEGY,
            "signal_date": signal,
            "target": target,
            "due": due,
            "status": "ready",
            "cutoff": cutoff.isoformat(),
            "price_and_master_sources": [],
            "jsf_sources": [],
            "picks": [{"code": "10000", "rank": 1, "prev_close": "100", "atr": "5"}]
            if signal == START
            else [],
        }

    def quotes(root, days, asof):
        return (
            {
                "10000": [
                    dict(
                        Date=days[0],
                        Code="10000",
                        O=100,
                        H=102,
                        L=99,
                        C=101,
                        AdjFactor=1,
                        ExRT=None,
                    )
                ]
            },
            {},
            [],
        )

    monkeypatch.setattr(plan, "calendar_days", calendar)
    monkeypatch.setattr(plan, "prepare", screen)
    monkeypatch.setattr(plan, "dated_inputs", quotes)
    return tmp_path / "data/plan-forward/accounts", calls


def test_independent_fixed_deadlines_reuse_frozen_candidates_then_sell_once(family, tmp_path):
    accounts, calls = family
    start = date.fromisoformat(START)
    days = [str(d) for n in range(25) if (d := start + timedelta(days=n)).weekday() < 5]
    frozen = {}
    for i, day in enumerate(days[:12]):
        rows = plan_forward.run_due(tmp_path, now=clock(day))["plans"]
        assert set(rows) == {"A", "A9", "A10"}
        if i == 0:
            for key, (_, term) in plan.FIXED_TERMS.items():
                path = accounts / key / "sessions" / days[1] / "decision.json"
                frozen[key] = path.read_bytes()
                decision = json.loads(frozen[key])
                assert decision["strategy"] == plan.STRATEGY_BY_TERM[term]
                assert decision["due"] == days[1 + term]
                if key != "A":
                    assert decision["selection_source"] == {
                        "account": "A",
                        "decision_sha256": plan.swing.signature(json.loads(frozen["A"])),
                    }
        for key, (_, term) in plan.FIXED_TERMS.items():
            row = rows[key]
            assert row["status"] == "waiting_close"
            if i:
                assert ("10000" in row["positions"]) == (1 <= i < 1 + term)
                assert Decimal(row["cash"]) == (
                    Decimal("2989985") if i < 1 + term else CAPITAL - 30
                )
                path = accounts / key / "sessions" / day / "close.json"
                actions = [event["action"] for event in plan.swing.read(path)["events"]]
                assert actions == (["BUY"] if i == 1 else ["SELL_TIME"] if i == term + 1 else [])
            assert (accounts / key / "sessions" / days[1] / "decision.json").read_bytes() == frozen[
                key
            ]
    assert calls == days[:12]  # A single screen per decision across all three accounts.
    files = {p: p.read_bytes() for p in accounts.rglob("*.json")}
    plan_forward.run_due(tmp_path, now=clock(days[11], 20))
    assert files == {p: p.read_bytes() for p in accounts.rglob("*.json")}


def test_local_stop_and_missed_deadline_are_independent_of_ready_sibling(family, tmp_path):
    accounts, _ = family
    target = "2026-11-26"
    plan.advance(tmp_path, accounts / "A", start=START, capital=CAPITAL, now=clock(START))
    book = accounts / "A9"
    book.mkdir(parents=True)
    (book / "STOP").touch()
    stopped = plan.advance(tmp_path, book, start=START, capital=CAPITAL, term=9, now=clock(START))
    assert stopped["decision_status"] == "stopped" and not stopped["picks"]
    missed = plan.advance(
        tmp_path, accounts / "A10", start=START, capital=CAPITAL, term=10, now=clock(target, 9)
    )
    assert missed["decision_status"] == "missed_preopen" and not missed["picks"]
    assert plan.swing.read(accounts / "A/sessions" / target / "decision.json")["picks"]


def test_version_and_state_cannot_be_crossed(family, tmp_path):
    accounts, _ = family
    with pytest.raises(ValueError, match="version_mismatch"):
        plan.account_options(dict(id="A9", version=plan.FIXED_TERMS["A10"][0]))
    book = accounts / "A9"
    plan.advance(tmp_path, book, start=START, capital=CAPITAL, term=9, now=clock(START))
    with pytest.raises(ValueError, match="configuration_changed"):
        plan.advance(tmp_path, book, start=START, capital=CAPITAL, term=10, now=clock(START))


def test_first_ready_variant_preserves_rankings_when_original_resumes_after_revision(
    family, tmp_path, monkeypatch
):
    accounts, calls = family
    target = "2026-11-26"

    def decision_path(key):
        return accounts / key / "sessions" / target / "decision.json"

    plan.advance(tmp_path, accounts / "A9", start=START, capital=CAPITAL, term=9, now=clock(START))
    original = plan.swing.read(decision_path("A9"))

    # A later input revision would select a different name if another screen were run.
    def revised(project, signal, target, due, cutoff):
        return original | {
            "due": due,
            "picks": [{"code": "20000", "rank": 1, "prev_close": "100", "atr": "5"}],
        }

    monkeypatch.setattr(plan, "prepare", revised)
    (accounts / "A9/STOP").touch()
    stopped = plan.advance(
        tmp_path, accounts / "A9", start=START, capital=CAPITAL, term=9, now=clock(target, 7)
    )
    assert stopped["decision_status"] == "stopped_preopen"
    for key, term, peer in (("A", 6, "A9"), ("A10", 10, "A")):
        row = plan.advance(
            tmp_path,
            accounts / key,
            start=START,
            capital=CAPITAL,
            term=term,
            now=clock(target, 8) + timedelta(minutes=term),
        )
        assert row["decision_status"] == "ready" and row["picks"] == original["picks"]
        frozen = plan.swing.read(decision_path(key))
        assert frozen["due"] != original["due"]
        assert frozen["selection_source"] == {
            "account": peer,
            "decision_sha256": plan.swing.signature(plan.swing.read(decision_path(peer))),
        }
    assert calls == [START]


def test_retired_fixed_accounts_keep_pending_decisions_without_processing_them(family, tmp_path):
    accounts, _ = family
    original = plan_forward.run_due(tmp_path, now=clock(START))["plans"]
    config = tmp_path / "config/forward-plans.json"
    registry = plan_forward.read(config)
    frozen = {}
    for row in registry["plans"]:
        key = row["id"]
        if key in ("A9", "A10"):
            row["stage"] = "retired"
            book = accounts / key
            frozen[key] = {p.relative_to(book): p.read_bytes() for p in book.rglob("*.json")}
    plan_forward.save(config, registry)
    for day in ("2026-11-26", "2026-11-27"):
        result = plan_forward.run_due(tmp_path, now=clock(day), recovery_signal=START)
        assert set(result["plans"]) == {"A"}
        for key, expected in frozen.items():
            archive = accounts.parent / "retired" / (key + "-" + original[key]["version"])
            assert plan_forward.read(archive / "retirement.json")["final_report"] == original[key]
            book = archive / "account"
            assert {p.relative_to(book): p.read_bytes() for p in book.rglob("*.json")} == expected
            assert not (accounts / key).exists()


def test_rejected_version_does_not_enroll_an_invalid_account(family, tmp_path):
    accounts, _ = family
    path = tmp_path / "config/forward-plans.json"
    catalog = path.read_text()
    path.write_text(catalog.replace(plan.FIXED_TERMS["A9"][0], "mislabeled"))
    bad = plan_forward.run_due(tmp_path, now=clock(START))
    assert bad["plans"]["A9"]["issue"] == "plan_a_version_mismatch"
    assert not (accounts / "A9/enrollment.json").exists()
    path.write_text(catalog)
    retried = plan_forward.run_due(tmp_path, now=clock(START, 20))
    assert retried["plans"]["A9"]["decision_status"] == "ready"
    assert (
        plan.swing.read(accounts / "A9/enrollment.json")["spec"]["version"]
        == plan.FIXED_TERMS["A9"][0]
    )
