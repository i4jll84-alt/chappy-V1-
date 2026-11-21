"""Isolated, evidence-gated replay of the frozen A0/B0 research accounts."""

import argparse
import csv
import hashlib
import importlib
import json
import re
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

ANCHOR = "2016-11-18"
FIRST5_END = "2021-11-19"
END = "2026-11-20"
SEAM = "2023-10-31"
HALT = "2020-10-01"
CAPITAL = 3_000_000
B_FILE = Path("runs/ab-structure-20161120-20261120/b/comparison_input.json")
B_SHA = "7aac78e7c80df5bb2231389b34b6a5de5904eea0b5897ae82b2d75115cb76e83"
NOTES = [
    "後日取得履歴による近似研究。過去時点の配信版・公表時刻は復元できない。",
    "前半の日証金制限措置は未収録のため空欄通過を仮定。残高欠測はゼロ補完しない。",
    "IRBANKの翌営業日更新を当日申込日の判定に使用するため、当時利用可能性は未証明。",
    "Aは100株固定、T+2まで売却金を拘束。費用は片道10円＋0.05%、悪化はさらに0.10%の滑り。",
    "B0は保存済みT+2口座を使用。Bの原費用は片道滑り0.10%＋手数料0.05%。",
    "期限始値欠測は買値仮決済、終値欠測は買値評価というA原版の例外を維持。件数を別記。",
    "税・配当・実際の板・呼値・寄付約定を再現した実取引成績ではない。",
    "休場境界は直前立会日引け。2020-10-01を除き、連続口座の現金・建玉を区間で引き継ぐ。",
]


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda x: str(x) if isinstance(x, Decimal) else asdict(x),
    ).encode("utf-8")


def read(path):
    return json.loads(Path(path).read_bytes())


def waiting(scope, reason, **details):
    return {"state": "waiting", "scope": scope, "reason": reason, **details}


@contextmanager
def imports(project, lab):
    paths = [str(lab), str(project / "scripts/swing_gap_atr"), str(project / "src")]
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path[:0] = paths
    try:
        names = [
            "structure_a",
            "structure_a_inputs",
            "structure_metrics",
            "tenyear",
            "native_a",
            "decade_a",
        ]
        modules = {name: importlib.import_module("strategy_lab." + name) for name in names}
        for module in modules.values():
            if Path(module.__file__).resolve().parent != lab / "strategy_lab":
                raise ValueError("another_research_checkout_loaded")
        yield modules
    finally:
        sys.dont_write_bytecode = old
        for path in paths:
            sys.path.remove(path)


def history(project):
    """Verify the archived market inputs before using either a fresh or cached result."""
    import duckdb

    root = project / "data/jquants/history"
    manifest = root / "quality-20261031.json"
    meta = read(manifest)
    if any(meta["checks"].values()):
        raise ValueError("history_quality_failed")
    pins = {str(manifest): digest(manifest)}
    calendars = []
    for item in meta["input_manifest"]:
        if item["endpoint"] not in (
            "/equities/bars/daily",
            "/equities/master",
            "/markets/calendar",
        ):
            continue
        path = root / item["parquet_path"]
        actual = digest(path)
        if item["state"] != "complete" or actual != item["parquet_sha256"]:
            raise ValueError("market_archive_changed")
        pins[str(path)] = actual
        if item["endpoint"] == "/markets/calendar":
            calendars.append(str(path))
    with duckdb.connect() as db:
        db.from_parquet(calendars).create_view("calendar")
        days = [
            r[0]
            for r in db.execute(
                "SELECT Date FROM calendar WHERE HolDiv IN ('1','2') ORDER BY Date"
            ).fetchall()
            if r[0] != HALT
        ]
    if len(days) != len(set(days)) or any(d not in days for d in (ANCHOR, FIRST5_END, END)):
        raise ValueError("invalid_exchange_calendar")
    if days.index(ANCHOR) < 20 or days.index(END) + 2 >= len(days):
        raise ValueError("calendar_warmup_or_settlement_missing")
    return days, pins


def decisions(root, days, end):
    """A receipt is usable only when every tested rank has a resolved credit decision."""
    ranks, pins, missing = {}, {}, []
    left, right = days.index(ANCHOR), days.index(end)
    for i in range(left, right):
        day = days[i]
        if day > SEAM:
            continue
        path = root / "required_decisions" / (day + ".json")
        if not path.is_file():
            missing.append({"date": day, "reason": "decision_missing"})
            continue
        data = read(path)
        selected, tested = data.get("selected"), data.get("tested")
        if data.get("date") != day or data.get("previous") != days[i - 1]:
            raise ValueError("decision_calendar_mismatch:" + day)
        if data.get("complete") is not True:
            missing.append({"date": day, "reason": "decision_incomplete"})
            continue
        if not isinstance(selected, list) or not isinstance(tested, list):
            raise ValueError("decision_schema_mismatch:" + day)
        codes = [r.get("code") for r in tested]
        if (
            len(selected) > 5
            or len(codes) != len(set(codes))
            or any(not isinstance(c, str) or not re.fullmatch(r"[0-9A-Z]{4}0", c) for c in codes)
            or any(r.get("state") not in {"pass", "fail", "no_data"} for r in tested)
            or any(not isinstance(r.get("sources"), list) for r in tested)
        ):
            raise ValueError("invalid_tested_ranks:" + day)
        if any(r["state"] == "no_data" for r in tested):
            missing.append({"date": day, "reason": "unresolved_credit_balance"})
            continue
        passed = [r["code"] for r in tested if r["state"] == "pass"]
        if passed != selected or len(passed) > 5:
            raise ValueError("selected_ranks_mismatch:" + day)
        pins[str(path)] = digest(path)
        ranks[day] = selected
    return ranks, pins, missing


def check_seam(root, codes, official):
    """Do not bridge October/November using incompatible vendor balances."""
    conflicts, pins = [], {}
    for code in sorted(set(codes)):
        path = root / (code[:-1] + ".json")
        row = read(path).get("days", {}).get(SEAM) if path.is_file() else None
        current = official.get(code)
        try:
            expected = tuple(
                int(current[k].replace(",", "")) for k in ("融資残高（株）", "貸株残高（株）")
            )
        except (KeyError, AttributeError, TypeError, ValueError):
            expected = None
        actual = (row.get("loan_balance"), row.get("stock_loan_balance")) if row else None
        if actual is None or expected is None or actual != expected:
            conflicts.append({"code": code, "irbank": actual, "official": expected})
        elif not all(type(v) is int and v >= 0 for v in actual):
            raise ValueError("invalid_seam_balance")
        if path.is_file():
            pins[str(path)] = digest(path)
    return conflicts, pins


def official_ranks(project, root, modules):
    """Reconstruct the official suffix and retain all its frozen-rank checks."""
    from trading_system.research import swing_credit
    from trading_system.research.daytrade_universe import dated_inputs

    owner = importlib.import_module("nojsf_decade_ranks")
    if Path(owner.__file__).resolve().parent != project / "scripts/swing_gap_atr":
        raise ValueError("another_native_checkout_loaded")
    frozen, proofs = modules["decade_a"]._ranks(project)
    source = modules["structure_a_inputs"]
    pools, _, evidence = source._recover(project, frozen, swing_credit, dated_inputs, owner)
    ranks = {day: source.select(pool, Decimal(200000)) for day, pool in pools.items() if day > SEAM}
    for day, selected in ranks.items():
        if day in frozen and selected != frozen[day]:
            raise ValueError("official_rank_differs_from_frozen:" + day)
    # Check the entire tested technical prefix, including failed credit candidates:
    # a vendor discrepancy in an excluded higher rank can change the top five.
    from fractions import Fraction

    import duckdb

    with duckdb.connect() as db:
        db.execute("SET threads=2")
        dates, _ = source._tables(project, owner, db, dated_inputs)
        i = dates.index("2023-11-01")
        technical = db.execute(
            owner.FEATURES, [dates[i - 20], dates[i], dates[i], dates[i]]
        ).fetchall()
    ordered = sorted(technical, key=lambda r: (-Fraction(r[2]) / Fraction(r[3]), r[1]))
    seam_codes = []
    last_selected = ranks["2023-11-01"][-1] if len(ranks["2023-11-01"]) == 5 else None
    for row in ordered:
        seam_codes.append(row[1])
        if row[1] == last_selected:
            break
    official_base = project / "data/free-market/jsf-history"
    meta_path = official_base / "days" / (SEAM + ".json")
    meta = read(meta_path)
    csv_path = official_base / meta["csv"]["path"]
    if digest(csv_path) != meta["csv"]["sha256"]:
        raise ValueError("official_seam_hash_mismatch")
    conflicts, seam_pins = check_seam(root, seam_codes, swing_credit.tokyo(csv_path.read_bytes()))
    return ranks, {"frozen": proofs, "official": evidence, "seam": seam_pins}, conflicts


def load_market(project, codes, modules, days, end):
    engine, _, market_class = modules["native_a"]._native(project)
    market = market_class.DecadeMarket(project, codes)
    market.sessions = days
    market.indices = {day: i for i, day in enumerate(days)}
    tail, refs = {}, {}
    if end > "2026-11-18":
        from trading_system.research.daytrade_universe import dated_inputs

        for day in days[days.index("2026-11-18") + 1 : days.index(end) + 1]:
            quotes, _, evidence = dated_inputs(
                project / "data/jquants", [day], datetime.fromisoformat(day + "T23:59:59+09:00")
            )
            tail[day] = {c: value[0] for c, value in quotes.items() if c in codes}
            refs[day] = evidence
    original = market.bars
    market.bars = lambda day: tail[day] if day in tail else original(day)
    return engine, market, refs


def tail_evidence(project, days, end):
    if end <= "2026-11-18":
        return {}
    from trading_system.research.daytrade_universe import dated_inputs

    refs = {}
    for day in days[days.index("2026-11-18") + 1 : days.index(end) + 1]:
        _, _, refs[day] = dated_inputs(
            project / "data/jquants", [day], datetime.fromisoformat(day + "T23:59:59+09:00")
        )
    return refs


def verify_replays(first, second, first_proof, second_proof):
    if encode(first) != encode(second) or first_proof != second_proof:
        raise ValueError("independent_replays_disagree")


def replay_once(project, modules, days, ranks, end):
    owner = modules["structure_a"]
    books = {
        "A0_base": owner.Account(owner.Rules()),
        "A0_base_slip": owner.Account(owner.Rules(slip=Decimal("0.001"))),
    }
    codes = {c for values in ranks.values() for c in values}
    engine, market, tail = load_market(project, codes, modules, days, end)
    if engine.FEE != Decimal(10) or engine.SLIP != Decimal("0.0005"):
        raise ValueError("native_A_costs_changed")
    for book in books.values():
        book.equity[ANCHOR] = {
            "nav": float(book.nav),
            "exposed": False,
            "cash": float(book.cash),
            "available_cash": float(book.cash),
            "positions": 0,
        }
    try:
        for i in range(days.index(ANCHOR), days.index(end)):
            signal, day = days[i : i + 2]
            picks = engine.reference_list(market, signal, ranks[signal])
            bars = market.bars(day)
            for book in books.values():
                owner.advance(book, i + 1, days, picks, bars, engine, lambda _: None)
        proof = {"used_prices": dict(market.used_hashes), "tail": tail}
    finally:
        market.db.close()
        type(market).bars.cache_clear()
        market.monthly.cache_clear()
        market.atr.cache_clear()
    payload = {}
    for name, book in books.items():
        payload[name] = {
            "equity": book.equity,
            "trades": book.trades,
            "buys": book.buys,
            "counters": dict(book.counts),
            "closing_positions": book.positions,
            "pending_settlements": book.pending,
        }
    return json.loads(encode(payload)), proof


def tables(cases, modules, days, scope):
    metrics = modules["structure_metrics"]
    grid = modules["tenyear"].windows(END)
    if scope == "first5":
        grid = [w for w in grid if w["id"] == "first5"]
    calendar = days[days.index(ANCHOR) : days.index(END) + 1]
    periods = {name: metrics.measure(case, grid, calendar) for name, case in cases.items()}
    portfolio = {
        "A0_B0": metrics.paired(cases["A0_base"], cases["B0_base"], grid, calendar),
        "A0_slip_B0": metrics.paired(cases["A0_base_slip"], cases["B0_base"], grid, calendar),
    }
    return {"periods": periods, "portfolio": portfolio, "windows": grid}


def daily_rows(cases, end=FIRST5_END):
    left = cases["A0_base"]["equity"]
    rows = []
    for day in sorted(d for d in left if d <= end):
        values = {name: case["equity"][day]["nav"] for name, case in cases.items()}
        rows.append(
            {
                "date": day,
                **values,
                "A0_B0": values["A0_base"] + values["B0_base"],
                "A0_slip_B0": values["A0_base_slip"] + values["B0_base"],
            }
        )
    return rows


def write_csv(path, rows):
    if not rows:
        return
    columns = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def cached_result(target, identity):
    if not target.exists():
        return None
    manifest = target / "manifest.json"
    if not manifest.is_file():
        raise ValueError("existing_output_incomplete_no_overwrite")
    meta = read(manifest)
    if meta.get("input_sha256") != identity:
        raise ValueError("existing_output_inputs_differ_no_overwrite")
    required = {"report.json", "accounts.json", "periods.csv", "portfolio.csv", "first5-nav.csv"}
    if meta.get("state") != "complete" or set(meta.get("outputs", {})) != required:
        raise ValueError("existing_output_manifest_invalid")
    for name, expected in meta["outputs"].items():
        path = target / name
        if not path.is_file() or digest(path) != expected:
            raise ValueError("existing_output_corrupt_no_overwrite")
    report = read(target / "report.json")
    scope = report.get("scope")
    if (
        scope not in {"first5", "full"}
        or report.get("state") != "complete"
        or report.get("input_sha256") != identity
    ):
        raise ValueError("existing_output_report_invalid")
    return {
        "state": "complete",
        "scope": scope,
        "reused": True,
        "output": str(target),
        "input_sha256": identity,
    }


def run(project, lab, scope="auto"):
    project, lab = Path(project).resolve(), Path(lab).resolve()
    automatic = scope == "auto"
    if scope not in {"auto", "first5", "full"}:
        raise ValueError("invalid_scope")
    if any("onedrive" in p.lower() for path in (project, lab) for p in path.parts):
        raise ValueError("local_non_onedrive_paths_required")
    root = project / "data/irbank_jsf"
    audit = root / "audit-report.json"
    if not audit.is_file() or read(audit).get("state") != "checked":
        return waiting(scope, "official_audit_not_checked")
    days, price_pins = history(project)
    checks = {}
    for name in ("first5", "full") if scope == "auto" else (scope,):
        checks[name] = decisions(root, days, FIRST5_END if name == "first5" else END)
    if scope == "auto":
        scope = "full" if not checks["full"][2] else "first5"
    ranks, decision_pins, missing = checks[scope]
    if missing:
        return waiting(
            scope,
            "required_decisions_not_ready",
            missing_count=len(missing),
            first_missing=missing[:10],
        )
    end = FIRST5_END if scope == "first5" else END
    b_path = lab / B_FILE
    if digest(b_path) != B_SHA:
        raise ValueError("frozen_B0_input_changed")
    baseline = read(b_path)["cases"]["B0_base"]
    expected = days[days.index(ANCHOR) : days.index(END) + 1]
    if sorted(baseline["equity"]) != expected or baseline["equity"][ANCHOR]["nav"] != CAPITAL:
        raise ValueError("frozen_B0_calendar_or_capital_changed")
    evidence = {
        "scope": scope,
        "start": ANCHOR,
        "end": end,
        "audit": digest(audit),
        "decisions": decision_pins,
        "market": price_pins,
        "B0": B_SHA,
    }
    with imports(project, lab) as modules:
        if scope == "full":
            suffix, official_evidence, conflicts = official_ranks(project, root, modules)
            if conflicts:
                if automatic:
                    # A suffix seam must not prevent an already complete first-half study.
                    return run(project, lab, "first5")
                return waiting(
                    scope, "october31_cross_source_balance_unresolved", conflicts=conflicts
                )
            ranks.update(suffix)
            evidence["official_suffix"] = official_evidence
        signal_days = days[days.index(ANCHOR) : days.index(end)]
        if sorted(ranks) != signal_days:
            raise ValueError("decision_tape_not_contiguous")
        # Include every local research module because transitive accounting imports matter.
        paths = [
            *sorted((lab / "strategy_lab").glob("*.py")),
            *sorted((project / "scripts/swing_gap_atr").glob("*.py")),
            Path(__file__),
        ]
        evidence["implementation"] = {str(p): digest(p) for p in paths}
        evidence["tail_inputs"] = tail_evidence(project, days, end)
        evidence["ranks_sha256"] = hashlib.sha256(encode(ranks)).hexdigest()
        identity = hashlib.sha256(encode(evidence)).hexdigest()
        target = project / "data/offline-verification/irbank-a-restore-20261121" / scope
        existing = cached_result(target, identity)
        if existing:
            return existing
        first, market_proof = replay_once(project, modules, days, ranks, end)
        second, second_proof = replay_once(project, modules, days, ranks, end)
        verify_replays(first, second, market_proof, second_proof)
        first["B0_base"] = {
            "equity": {d: r for d, r in baseline["equity"].items() if d <= end},
            "trades": [r for r in baseline["trades"] if r["exit_date"] <= end],
        }
        report = {
            "state": "complete",
            "scope": scope,
            "input_sha256": identity,
            "start": ANCHOR,
            "end": end,
            "notes": NOTES,
            "replay_match": "two fresh accounts using the same implementation: "
            "all equity/trades/buys/counters/end-state matched exactly",
            "evidence": evidence,
            "market_proof": market_proof,
            "exceptions": {k: v.get("counters", {}) for k, v in first.items()},
            **tables(first, modules, days, scope),
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="irbank-replay-", dir=target.parent) as temporary:
        stage = Path(temporary) / "result"
        stage.mkdir()
        for name, value in (("report.json", report), ("accounts.json", first)):
            (stage / name).write_bytes(encode(value))
        write_csv(
            stage / "periods.csv",
            [dict(case=k, **r) for k, cases in report["periods"].items() for r in cases.values()],
        )
        write_csv(
            stage / "portfolio.csv",
            [
                dict(case=k, window=w, **r)
                for k, periods in report["portfolio"].items()
                for w, r in periods.items()
            ],
        )
        rows = daily_rows(first)
        write_csv(stage / "first5-nav.csv", rows)
        meta = {
            "state": "complete",
            "input_sha256": identity,
            "outputs": {p.name: digest(p) for p in stage.iterdir()},
        }
        (stage / "manifest.json").write_bytes(encode(meta))
        stage.rename(target)
    return {
        "state": "complete",
        "scope": scope,
        "output": str(target),
        "input_sha256": identity,
        "reused": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--lab", type=Path)
    parser.add_argument("--scope", choices=("first5", "full", "auto"), default="auto")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    lab = args.lab or project.parent / "個別株自動売買ツールV1トレード手法検討バックテスト用"
    result = run(project, lab, args.scope)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
