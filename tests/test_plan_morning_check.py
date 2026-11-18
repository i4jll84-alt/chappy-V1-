import hashlib
import json
from datetime import datetime

import duckdb
import pytest

from trading_system import plan_morning_check as check
from trading_system.research import a0_5 as native
from trading_system.research import a0_5_book as ledger

SIGNAL = "2026-11-19"
TARGET = "2026-11-20"
CAPITAL = 3_000_000


def at(clock, day=TARGET):
    return datetime.fromisoformat(day + "T" + clock + "+09:00")


def put(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def run(project, checked="07:55", started="07:40", target=TARGET, signal=SIGNAL):
    report = {
        "recovery": {
            "started_at": at(started, target).isoformat(),
            "target": signal,
            "next_session": target,
        }
    }
    return check.record_startup(project, report, now=at(checked, target))


def save_b(project, doc):
    account = native.root_for(project, CAPITAL)
    state = ledger.load(account, CAPITAL)
    state["plans"][SIGNAL] = doc
    ledger.save(account, state)


@pytest.fixture
def sample(tmp_path):
    project = tmp_path
    store = project / "data/plan-forward"
    specs = [dict(id=k, capital=CAPITAL, stage="active", start=SIGNAL) for k in "AB"]
    put(project / "config/forward-plans.json", {"plans": specs})
    account = native.root_for(project, CAPITAL)
    state = ledger.initial(CAPITAL)
    ledger.save(account, state)
    (native.root_for(project) / "enabled").touch()
    put(
        store / "accounts/B/baseline.json",
        dict(
            start=SIGNAL,
            initial_capital=CAPITAL,
            native_revision=1,
            native_revision_sha256=ledger.revision_digest(account / "ledger.sqlite", 1),
        ),
    )
    jq = project / "data/jquants"
    manifest = jq / "daily-snapshots" / SIGNAL / "v1/manifest.json"
    raw = {"observed_at": at("07:41").isoformat(), "available_at": at("07:41:01").isoformat()}
    put(manifest, {"pages": [raw]})
    put(
        manifest.parent.parent / "last_collection.json",
        dict(date=SIGNAL, daily_manifest=str(manifest), jobs={"master": {"job_id": "m1"}}),
    )
    master = jq / "reference/master/catalog.duckdb"
    master.parent.mkdir(parents=True)
    with duckdb.connect(str(master)) as db:
        db.execute(
            "CREATE TABLE pages(job_id VARCHAR,raw_path VARCHAR,"
            "raw_sha256 VARCHAR,observed_at TIMESTAMPTZ)"
        )
        db.execute("INSERT INTO pages VALUES('m1','official','test-digest',?)", [at("07:42")])
    common = [
        dict(
            date=SIGNAL,
            manifest=str(manifest.relative_to(jq)),
            sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            completed_at=at("07:42").isoformat(),
        ),
        dict(master_job="m1", page="official", sha256="test-digest"),
    ]
    a = dict(
        status="ready",
        signal_date=SIGNAL,
        target=TARGET,
        picks=[],
        recorded_at=at("07:46").isoformat(),
        cutoff=at("07:45").isoformat(),
        price_and_master_sources=common,
        decision_origin="morning_recovery",
        jsf_sources=[
            dict(date=SIGNAL, available_at=at("07:44").isoformat()),
            dict(date="2026-11-18", available_at=at("13:00", SIGNAL).isoformat()),
        ],
    )
    b = dict(
        state="ready",
        date=SIGNAL,
        order_date=TARGET,
        candidates=[],
        orders=[],
        recorded_at=at("07:49:41").isoformat(),
        cutoff=at("07:48").isoformat(),
        inputs=[
            *common,
            *[
                dict(date=SIGNAL, dataset=ds, observed_at=at("07:41").isoformat())
                for ds in ("financials", "topix")
            ],
            dict(date=SIGNAL, daily_receipt="test", completed_at=at("07:42").isoformat()),
        ],
    )
    return store / "accounts/A/sessions" / TARGET / "decision.json", a, b


def test_two_plans_zero_candidates_actual_morning_retry_and_cli(tmp_path, sample, capsys):
    path, a, b = sample
    pending = run(tmp_path, checked="07:40")
    assert pending["state"] == "pending"
    put(path, a)
    save_b(tmp_path, b)
    result = run(tmp_path, checked="08:21", started="08:20")
    assert result["state"] == "met"
    assert result["started_at"] == at("07:40").isoformat()
    assert result["decision_at"] == b["recorded_at"] and result["seconds_to_open"] == 4219
    assert all(r["candidate_count"] == 0 for r in result["plans"].values())
    assert result["plans"]["A"]["order_count"] is None
    assert result["plans"]["B"]["order_count"] == 0
    assert run(tmp_path, checked="20:11", started="20:10") == result
    check.main(["--project", str(tmp_path), "--date", TARGET])
    assert json.loads(capsys.readouterr().out) == result
    path.unlink()
    assert run(tmp_path, checked="10:00", started="09:50") == result


@pytest.mark.parametrize(
    "case,state",
    [
        ("prior_jsf", "prior_inputs"),
        ("prior_master", "prior_inputs"),
        ("night_decision", "preexisting"),
        ("night_old_format", "preexisting"),
        ("deadline", "missed"),
        ("ahead_of_cutoff", "unverified"),
        ("no_timezone", "unverified"),
        ("wrong_entry", "unverified"),
        ("old_receipt_hash", "unverified"),
        ("earnings_unverified", "unverified"),
        ("evening", "evening_run"),
        ("disabled", "stopped"),
        ("actual_stop", "stopped"),
        ("account_stop", "stopped"),
        ("missing_after_open", "missed"),
        ("corrupt_native", "unverified"),
    ],
)
def test_provenance_and_deadlines(tmp_path, sample, case, state):
    path, a, b = sample
    checked = "09:02"
    if case == "prior_jsf":
        a["jsf_sources"][0]["available_at"] = at("20:10", SIGNAL).isoformat()
    if case == "prior_master":
        with duckdb.connect(str(tmp_path / "data/jquants/reference/master/catalog.duckdb")) as db:
            db.execute("UPDATE pages SET observed_at=?", [at("07:37")])
    if case in ("night_decision", "night_old_format"):
        b["cutoff"] = at("20:10", SIGNAL).isoformat()
        b["recorded_at"] = at("20:12", SIGNAL).isoformat()
        if case == "night_old_format":
            del b["recorded_at"]
    if case == "deadline":
        a["recorded_at"] = at("09:00").isoformat()
    if case == "ahead_of_cutoff":
        b["inputs"][-1]["completed_at"] = at("07:49").isoformat()
    if case == "no_timezone":
        a["recorded_at"] = "2026-11-20T07:55:00"
    if case == "wrong_entry":
        b["order_date"] = SIGNAL
    if case == "old_receipt_hash":
        a["price_and_master_sources"][0]["sha256"] = "corrupt"
    if case == "earnings_unverified":
        b["candidates"] = [{"Code": "12340"}]
    if case == "evening":
        put(tmp_path / "data/plan-forward/recovery/evening" / (SIGNAL + ".json"), dict(date=SIGNAL))
    if case == "disabled":
        (native.root_for(tmp_path) / "enabled").unlink()
    if case == "actual_stop":
        put(
            path.parent / "preopen_stop.json",
            dict(target=TARGET, observed_at=at("08:02").isoformat()),
        )
    if case == "account_stop":
        put(
            native.root_for(tmp_path, CAPITAL) / "stops" / (TARGET + ".json"),
            dict(observed_at=at("08:02").isoformat()),
        )
    if case != "missing_after_open":
        put(path, a)
    save_b(tmp_path, b)
    if case == "corrupt_native":
        (native.root_for(tmp_path, CAPITAL) / "ledger.sqlite").write_bytes(b"damaged")
    result = run(tmp_path, checked=checked)
    assert result["state"] == state
    if case == "disabled":
        assert result["plans"]["B"]["decision_at"] == b["recorded_at"]
    if case == "deadline":
        assert result["plans"]["A"]["seconds_to_open"] == 0
    if case == "night_old_format":
        assert result["plans"]["B"]["decision_at"] is None


def test_different_session_and_before_plan_start(tmp_path, sample):
    prior = run(tmp_path, signal="2026-11-18", target=SIGNAL)
    assert prior["state"] == "outside"
    result = check.record_startup(
        tmp_path,
        {
            "recovery": {
                "target": SIGNAL,
                "next_session": TARGET,
                "started_at": at("07:40", SIGNAL).isoformat(),
            }
        },
        now=at("07:45", SIGNAL),
    )
    assert result == prior
    assert not (tmp_path / "data/plan-forward/morning-check" / (TARGET + ".json")).exists()


def test_native_new_evening_completion_is_measured_and_old_decision_kept(
    tmp_path, sample, monkeypatch
):
    _, _, b = sample
    evening = at("20:10", SIGNAL)
    record = dict(b, cutoff=evening.isoformat())
    del record["recorded_at"]
    monkeypatch.setattr(native, "assess", lambda *args: dict(record))
    monkeypatch.setattr(native, "apply_openings", lambda project, state, *args: state)
    ticks = iter([0, 18, 30])
    monkeypatch.setattr(native, "monotonic", lambda: next(ticks))
    first = native.run(tmp_path, now=evening, capital=CAPITAL)
    assert first["recorded_at"] == at("20:10:18", SIGNAL).isoformat()
    assert native.run(tmp_path, now=at("21:00", SIGNAL), capital=CAPITAL) == first
