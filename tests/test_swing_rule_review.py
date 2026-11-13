import json
from decimal import Decimal
from pathlib import Path

from trading_system.research import swing_forward as sf
from trading_system.research.swing_rule_review import save_review


def store(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf8")


def test_credit_day_pair_keeps_later_sales_and_ignores_later_entries(tmp_path):
    accounts = sf.initial()
    store(tmp_path / "initial.json", {"start": "2026-11-16", "accounts": accounts})

    def record(day, due, complete, new, sale, px):
        folder = tmp_path / "sessions" / day
        store(
            folder / "preopen.json",
            {"target": day, "exit_if_bought": due, "signal": {"credit_complete": complete}},
        )
        fills, metrics, coverage = {}, {}, {}
        for key, book in list(accounts.items()):
            rule = key.split("_")[1]
            orders = {"buy": new[rule], "sell": sale[rule]}
            quotes = {
                c: {"O": px, "C": px, "ExRT": None, "AdjFactor": 1}
                for c in set(book["positions"]) | set(orders["buy"])
            }
            accounts[key], fills[key], metrics[key] = sf.settle_account(
                book, orders, quotes, day, due
            )
            coverage[key] = "recorded" if complete or rule == "current" else "credit_missing"
        store(
            folder / "close.json",
            dict(target=day, fills=fills, metrics=metrics, accounts=accounts, coverage=coverage),
        )

    empty = {"current": [], "plus3": []}
    assert save_review(tmp_path, "2026-11-13")["settled_sessions"] == 0
    assert not (tmp_path / "reviews").exists()
    record("2026-11-16", "2026-11-25", True, {"current": ["A"], "plus3": ["B"]}, empty, 100)
    record("2026-11-17", "2026-11-26", False, {"current": ["Z"], "plus3": []}, empty, 300)
    record("2026-11-25", "2026-12-03", False, empty, {"current": ["A"], "plus3": ["B"]}, 120)
    pending = save_review(tmp_path, "2026-11-17")["matched_credit"]["agent"]
    assert pending["pending_sessions"] == 1 and pending["plus3_minus_current_yen"] is None
    report = save_review(tmp_path, "2026-11-25")
    assert save_review(tmp_path, "2026-11-25") == report
    for pair in report["matched_credit"].values():
        assert pair["qualifying_entry_sessions"] == pair["completed_sessions"] == 1
        assert {Decimal(n) for n in pair["net_yen"].values()} == {Decimal(1969)}
        assert pair["completed_buys"] == {"current": 1, "plus3": 1}
    assert report["accounts"]["machine_current"]["buys"] == 2
    assert report["accounts"]["machine_plus3"]["coverage"]["credit_missing"] == 2
    assert Decimal(report["accounts"]["machine_current"]["net_yen"]) < 0


def test_observed_no_trade_is_zero_only_after_horizon(tmp_path):
    root = Path(tmp_path)
    books = sf.initial()
    store(root / "initial.json", dict(start="2026-11-16", accounts=books))
    for d in ("2026-11-16", "2026-11-25"):
        folder = root / "sessions" / d
        store(
            folder / "preopen.json",
            dict(
                target=d,
                exit_if_bought="2026-11-25" if d == "2026-11-16" else "2026-12-03",
                signal={"credit_complete": True},
            ),
        )
        store(
            folder / "close.json",
            dict(
                target=d,
                accounts=books,
                fills={k: [] for k in books},
                metrics={k: None for k in books},
                coverage={
                    k: "recorded" if not k.startswith("agent") else "missed_preopen" for k in books
                },
            ),
        )
    report = save_review(root, "2026-11-25")
    assert report["matched_credit"]["machine"]["completed_sessions"] == 1
    assert report["matched_credit"]["machine"]["plus3_minus_current_yen"] == "0"
    assert report["matched_credit"]["agent"]["completed_sessions"] == 0
    assert report["matched_credit"]["agent"]["plus3_minus_current_yen"] is None
