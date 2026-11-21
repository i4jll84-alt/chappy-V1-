"""Fixed original-versus-approximate sensitivity study using saved official JSF only.

No IRBANK client, network request, production account, or parameter search is used.
This is an official-data proxy for IRBANK-like conditions, not an IRBANK replay.
"""

import argparse
import csv
import importlib
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from trading_system.research import irbank_replay as shared
from trading_system.research import swing_credit

ANCHOR = "2023-11-01"
END = "2026-11-19"
TAIL_START = "2026-10-30"
BASELINE = Path("runs/ab-structure-20161120-20261120/a")
PINS = {
    "comparison_input.json": "f54e90612ce1a344a812b222fbac6e4b37ffa756cf9a08e1d03991c127433c8b",
    "A0_base-entries.csv": "2e12c4b4e72d16aedc88ecb08fa87469af47df907be7bd7e7c0955ba91e248cb",
    "A0_base_slip-entries.csv": "9025c031edb457ae11473fc523e12804390df7192a3d11dfb5993f866d9ea949",
}
FIELDS = ("融資残高（株）", "貸株残高（株）", "差引残高（株）")
SCENARIOS = ("original", "original_slip", "approx", "approx_slip")
NOTES = [
    "公式保存CSVによるIRBANK相当条件の感応度比較。全銘柄IRBANK実データの再生ではない。",
    "原条件は当時の研究に固定された東証残高・制限措置・資料版を使用。過去配信時点の証明とは異なる。",
    "近似条件は公式CSVの全市場を銘柄別に合算し制限措置を空欄とする。"
    "2026-10-30以降は追加取得した後日版を使う。全銘柄についてIRBANKとの同値を証明したものではない。",
    "欠測残高をゼロ代入しない。必要残高を確認できない銘柄は原ルール同様に選外とし件数を記録。"
    "資料日そのものが不足する場合は成績を生成しない。",
    "各口座300万円・現物100株固定・T+2受渡。片道10円＋0.05%、悪化条件のみ追加滑り0.10%。",
    "上位5を先に固定し、翌寄付の見送り後に補充しない。口座・決済条件は固定し最適化しない。",
    "原版の期限始値欠測時の買値仮決済・終値欠測時の買値評価を維持し件数を表示する。",
    "税・配当・板・呼値・寄付約定の実現性は未検証。共通期間だけの比較であり、"
    "前半5年や全10年の復元結果ではない。",
    "近似の成績差は市場合算・資料版・制限除去の複合影響。個別要因の因果効果とは解釈しない。",
]


def balances(row):
    if row is None:
        return None
    values = []
    for field in FIELDS:
        raw = row.get(field)
        if not isinstance(raw, str):
            return None
        value = raw.replace(",", "").strip()
        if not re.fullmatch(r"-?[0-9]+", value):
            return None
        values.append(int(value))
    loan, stock, net = values
    if loan < 0 or stock < 0 or loan - stock != net:
        raise ValueError("invalid_official_balance_arithmetic")
    return tuple(values)


def aggregate(body):
    """Sum distinct market rows; one unknown component leaves the entire code unknown."""
    totals, invalid, seen = defaultdict(lambda: [0, 0, 0]), set(), set()
    markets = Counter()
    for row in swing_credit.records(body):
        code, market = row.get("銘柄コード"), row.get("市場区分")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Z]{4,5}", code) or not market:
            raise ValueError("invalid_official_code_or_market")
        normalized = code + "0" if len(code) == 4 else code
        if (normalized, market) in seen:
            raise ValueError("duplicate_official_code_market")
        seen.add((normalized, market))
        markets[market] += 1
        values = balances(row)
        if values is None:
            invalid.add(normalized)
            continue
        for i, value in enumerate(values):
            totals[normalized][i] += value
    if not seen:
        raise ValueError("empty_official_csv")
    rows = {
        code: {**dict(zip(FIELDS, map(str, values), strict=True)), "制限措置等": ""}
        for code, values in totals.items()
        if code not in invalid
    }
    return rows, {"markets": dict(markets), "unobserved_codes": sorted(invalid)}


def proxy_body(project, day):
    """Later tail files are mandatory; never silently fall back to the old vintage."""
    if day >= TAIL_START:
        folder = project / "data/irbank_jsf/diagnostics" / ("jsf-" + day.replace("-", ""))
        manifest, csv_path = folder / "receipt.json", folder / "all.csv"
        meta = shared.read(manifest)
        if meta.get("date") != day or not str(meta.get("url", "")).startswith(
            "https://www.taisyaku.jp/"
        ):
            raise ValueError("diagnostic_source_identity_mismatch:" + day)
        expected = meta["sha256"]
    else:
        base = project / "data/free-market/jsf-history"
        manifest = base / "days" / (day + ".json")
        meta = shared.read(manifest)
        if meta["date"] != day or meta["state"] != "checked":
            raise ValueError("historical_source_identity_mismatch:" + day)
        csv_path, expected = base / meta["csv"]["path"], meta["csv"]["sha256"]
    body = csv_path.read_bytes()
    if shared.digest(csv_path) != expected:
        raise ValueError("proxy_csv_hash_mismatch:" + day)
    return body, {str(manifest): shared.digest(manifest), str(csv_path): expected}


def select(pool, before, current, passes):
    selected, tested, missing = [], [], 0
    for code, _ in pool:
        a, b = before.get(code), current.get(code)
        observed = balances(a) is not None and balances(b) is not None
        if not observed:
            missing += 1
        accepted = observed and passes(a, b)
        tested.append(
            {"code": code, "state": "pass" if accepted else "fail" if observed else "no_data"}
        )
        if accepted:
            selected.append(code)
        if len(selected) == 5:
            break
    return selected, tested, missing


def change_reasons(tested, prior_original, original, prior_proxy, proxy):
    counts = Counter()
    for code in sorted({r["code"] for rows in tested for r in rows}):
        old, new = original.get(code), proxy.get(code)
        old_pair = (balances(prior_original.get(code)), balances(old))
        new_pair = (balances(prior_proxy.get(code)), balances(new))
        if old and old.get("制限措置等") not in ("", None):
            counts["restriction_removed"] += 1
        if any(v is None for v in (*old_pair, *new_pair)):
            counts["unobserved_balance"] += 1
        elif old_pair != new_pair:
            counts["market_or_vintage_balance_change"] += 1
    return dict(counts)


def prepare(project, lab, modules):
    import duckdb

    from trading_system.research.daytrade_universe import dated_inputs

    source = modules["structure_a_inputs"]
    owner = importlib.import_module("nojsf_decade_ranks")
    if Path(owner.__file__).resolve().parent != project / "scripts/swing_gap_atr":
        raise ValueError("another_native_checkout_loaded")
    frozen, frozen_pins = modules["decade_a"]._ranks(project)
    credit_pins = shared.read(
        project / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs/sources.json"
    )
    refresh = shared.read(project / modules["decade_a"].STUDY / "refreshed/sources.json")
    pools = {}
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        dates, price_refs = source._tables(project, owner, db, dated_inputs)
        dates = [d for d in dates if d != shared.HALT]
        first, end = dates.index(ANCHOR), dates.index(END)
        # The experiment starts well after the 2020 halt; all feature windows are
        # identical to the frozen study. FEATURES already includes the 200k lot cap.
        for i in range(first, end, 63):
            stop = min(i + 63, end)
            rows = db.execute(
                owner.FEATURES, [dates[i - 20], dates[stop - 1], dates[i], dates[stop - 1]]
            ).fetchall()
            grouped = defaultdict(list)
            for day, code, close, old, _ in rows:
                grouped[day].append((-Fraction(close) / Fraction(old), code, close))
            for day in dates[i:stop]:
                pools[day] = [(code, close) for _, code, close in sorted(grouped[day])]
    needed = dates[first - 1 : end]
    absent = []
    for day in needed:
        if day >= TAIL_START:
            folder = project / "data/irbank_jsf/diagnostics" / ("jsf-" + day.replace("-", ""))
            if not all((folder / name).is_file() for name in ("receipt.json", "all.csv")):
                absent.append(day)
    if absent:
        return {"state": "waiting", "reason": "later_official_tail_missing", "dates": absent}
    ranks = {"original": {}, "approx": {}}
    selection_rows, differences = [], []
    original_refs, proxy_refs, aggregate_info = {}, {}, {}
    previous_original = previous_proxy = None
    for day in needed:
        original = source._credit(project, day, swing_credit, credit_pins, refresh, original_refs)
        body, refs = proxy_body(project, day)
        proxy, info = aggregate(body)
        proxy_refs.update(refs)
        aggregate_info[day] = info
        if day >= ANCHOR:
            old, old_tested, old_missing = select(
                pools[day], previous_original, original, source._passes_credit
            )
            new, new_tested, new_missing = select(
                pools[day], previous_proxy, proxy, source._passes_credit
            )
            if day in frozen and old != frozen[day]:
                raise ValueError("original_rank_differs_from_frozen:" + day)
            ranks["original"][day], ranks["approx"][day] = old, new
            reasons = change_reasons(
                (old_tested, new_tested), previous_original, original, previous_proxy, proxy
            )
            row = {
                "date": day,
                "original": " ".join(old),
                "approx": " ".join(new),
                "changed": old != new,
                "original_missing_tested": old_missing,
                "approx_missing_tested": new_missing,
                "technical_candidates": len(pools[day]),
            }
            selection_rows.append(row)
            if old != new:
                differences.append(
                    {
                        **row,
                        "reasons_in_tested_prefix": reasons,
                        "original_tested": old_tested,
                        "approx_tested": new_tested,
                    }
                )
        previous_original, previous_proxy = original, proxy
    baseline_pins = {}
    for name, expected in PINS.items():
        path = lab / BASELINE / name
        if shared.digest(path) != expected:
            raise ValueError("frozen_original_study_changed:" + name)
        baseline_pins[str(path)] = expected
    return {
        "state": "ready",
        "dates": dates,
        "ranks": ranks,
        "selection_rows": selection_rows,
        "differences": differences,
        "evidence": {
            "price_refs": price_refs,
            "frozen_pins": frozen_pins,
            "original_jsf": original_refs,
            "proxy_files": proxy_refs,
            "aggregate_info": aggregate_info,
            "baseline_files": baseline_pins,
        },
    }


def replay_once(project, modules, dates, ranks):
    owner = modules["structure_a"]
    books = {
        name: owner.Account(
            owner.Rules(slip=Decimal("0.001") if name.endswith("_slip") else Decimal(0))
        )
        for name in SCENARIOS
    }
    codes = {c for scenario in ranks.values() for selected in scenario.values() for c in selected}
    engine, market, tail = shared.load_market(project, codes, modules, dates, END)
    if engine.FEE != Decimal(10) or engine.SLIP != Decimal("0.0005"):
        raise ValueError("original_costs_changed")
    for book in books.values():
        book.equity[ANCHOR] = {
            "nav": float(book.nav),
            "exposed": False,
            "cash": float(book.cash),
            "available_cash": float(book.cash),
            "positions": 0,
        }
    try:
        for i in range(dates.index(ANCHOR), dates.index(END)):
            signal, day = dates[i : i + 2]
            bars = market.bars(day)
            prepared = {
                key: engine.reference_list(market, signal, tape[signal])
                for key, tape in ranks.items()
            }
            for name, book in books.items():
                owner.advance(
                    book,
                    i + 1,
                    dates,
                    prepared[name.removesuffix("_slip")],
                    bars,
                    engine,
                    lambda _: None,
                )
        price_proof = {"used_prices": dict(market.used_hashes), "tail": tail}
    finally:
        market.db.close()
        type(market).bars.cache_clear()
        market.monthly.cache_clear()
        market.atr.cache_clear()
    result = {
        name: {
            "equity": book.equity,
            "trades": book.trades,
            "buys": book.buys,
            "counters": dict(book.counts),
            "closing_positions": book.positions,
            "pending_settlements": book.pending,
        }
        for name, book in books.items()
    }
    return json.loads(shared.encode(result)), price_proof


def verify_prefix(cases, baseline, entries):
    checked = {}
    for name, source in (("original", "A0_base"), ("original_slip", "A0_base_slip")):
        recorded = baseline["cases"][source]
        expected_equity = {d: r for d, r in recorded["equity"].items() if ANCHOR <= d <= END}
        expected_trades = [r for r in recorded["trades"] if ANCHOR < r["exit_date"] <= END]
        expected_entries = [r for r in entries[source] if ANCHOR < r["date"] <= END]
        for key, expected in (
            ("equity", expected_equity),
            ("trades", expected_trades),
            ("buys", expected_entries),
        ):
            if shared.encode(cases[name][key]) != shared.encode(expected):
                raise ValueError("original_prefix_mismatch:" + name + ":" + key)
        checked[name] = {
            "equity": len(expected_equity),
            "trades": len(expected_trades),
            "buys": len(expected_entries),
        }
    return checked


def baseline_entries(lab):
    result = {}
    for name in ("A0_base", "A0_base_slip"):
        with (lab / BASELINE / (name + "-entries.csv")).open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        result[name] = [
            {
                k: value
                if k in ("date", "code")
                else int(value)
                if k in ("rank", "shares")
                else float(value)
                for k, value in row.items()
            }
            for row in rows
        ]
    return result


def metrics(cases, modules, dates):
    grid = [{"id": "common", "label": "共通期間", "start": ANCHOR, "end": END}]
    calendar = dates[dates.index(ANCHOR) : dates.index(END) + 1]
    rows = {
        name: modules["structure_metrics"].measure(case, grid, calendar)["common"]
        for name, case in cases.items()
    }
    if any(row["coverage"] != "full" for row in rows.values()):
        raise ValueError("comparison_account_coverage_incomplete")
    fields = modules["structure_metrics"].FIELDS
    deltas = {
        cost: {
            field: rows["approx" + cost][field] - rows["original" + cost][field]
            if rows["approx" + cost][field] is not None
            and rows["original" + cost][field] is not None
            else None
            for field in fields
        }
        for cost in ("", "_slip")
    }
    return rows, deltas


def cached(target, identity):
    if not target.exists():
        return None
    path = target / "manifest.json"
    if not path.is_file():
        raise ValueError("existing_comparison_incomplete_no_overwrite")
    manifest = shared.read(path)
    if manifest.get("input_sha256") != identity or manifest.get("state") != "complete":
        raise ValueError("existing_comparison_inputs_differ_no_overwrite")
    expected = {
        "report.json",
        "accounts.json",
        "metrics.csv",
        "deltas.csv",
        "daily-nav.csv",
        "selections.csv",
    }
    if set(manifest.get("outputs", {})) != expected:
        raise ValueError("existing_comparison_manifest_invalid")
    for name, sha in manifest["outputs"].items():
        if shared.digest(target / name) != sha:
            raise ValueError("existing_comparison_output_changed")
    return {
        "state": "complete",
        "scope": "common_official_proxy",
        "reused": True,
        "output": str(target),
        "input_sha256": identity,
    }


def run(project, lab, output=None):
    import hashlib

    project, lab = Path(project).resolve(), Path(lab).resolve()
    target = (
        Path(output)
        if output
        else project / "data/offline-verification/irbank-original-vs-approx-20261121"
    ).resolve()
    if any("onedrive" in part.lower() for path in (project, lab, target) for part in path.parts):
        raise ValueError("non_onedrive_paths_required")
    allowed = (project / "data/offline-verification").resolve()
    if target == allowed or not target.is_relative_to(allowed):
        raise ValueError("output_must_be_new_local_research_subdirectory")
    with shared.imports(project, lab) as modules:
        data = prepare(project, lab, modules)
        if data["state"] != "ready":
            return data
        files = [
            Path(__file__),
            Path(shared.__file__),
            *sorted((lab / "strategy_lab").glob("*.py")),
            *sorted((project / "scripts/swing_gap_atr").glob("*.py")),
        ]
        evidence = {
            **data["evidence"],
            "start": ANCHOR,
            "end": END,
            "definition": "official_all_market_later_tail_no_restriction_proxy_v1",
            "implementation": {str(p): shared.digest(p) for p in files},
            "tail_prices": shared.tail_evidence(project, data["dates"], END),
            "ranks_sha256": hashlib.sha256(shared.encode(data["ranks"])).hexdigest(),
        }
        identity = hashlib.sha256(shared.encode(evidence)).hexdigest()
        prior = cached(target, identity)
        if prior:
            return prior
        first, prices = replay_once(project, modules, data["dates"], data["ranks"])
        second, second_prices = replay_once(project, modules, data["dates"], data["ranks"])
        shared.verify_replays(first, second, prices, second_prices)
        prefix = verify_prefix(
            first, shared.read(lab / BASELINE / "comparison_input.json"), baseline_entries(lab)
        )
        summary, deltas = metrics(first, modules, data["dates"])
        report = {
            "state": "complete",
            "scope": "common_official_proxy",
            "input_sha256": identity,
            "start": ANCHOR,
            "end": END,
            "notes": NOTES,
            "metrics": summary,
            "deltas": deltas,
            "selection_changed_days": len(data["differences"]),
            "decision_days": len(data["selection_rows"]),
            "selection_differences": data["differences"],
            "evidence": evidence,
            "price_proof": prices,
            "frozen_original_prefix_matched": prefix,
            "repeat_replay": "same implementation, fresh accounts; all records matched exactly",
            "exceptions": {k: v["counters"] for k, v in first.items()},
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="irbank-comparison-", dir=target.parent) as temporary:
        stage = Path(temporary) / "result"
        stage.mkdir()
        (stage / "report.json").write_bytes(shared.encode(report))
        (stage / "accounts.json").write_bytes(shared.encode(first))
        shared.write_csv(stage / "metrics.csv", [dict(scenario=k, **r) for k, r in summary.items()])
        shared.write_csv(
            stage / "deltas.csv",
            [dict(cost="stress" if k else "base", **r) for k, r in deltas.items()],
        )
        shared.write_csv(stage / "selections.csv", data["selection_rows"])
        shared.write_csv(
            stage / "daily-nav.csv",
            [
                {"date": day, **{name: case["equity"][day]["nav"] for name, case in first.items()}}
                for day in sorted(first["original"]["equity"])
            ],
        )
        manifest = {
            "state": "complete",
            "input_sha256": identity,
            "outputs": {p.name: shared.digest(p) for p in stage.iterdir()},
        }
        (stage / "manifest.json").write_bytes(shared.encode(manifest))
        stage.rename(target)
    return {
        "state": "complete",
        "scope": "common_official_proxy",
        "reused": False,
        "output": str(target),
        "input_sha256": identity,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--lab", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    project = args.project.resolve()
    lab = args.lab or project.parent / "個別株自動売買ツールV1トレード手法検討バックテスト用"
    result = run(project, lab, args.output)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["state"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
