"""Price monitor CLI preserves catalogs and reports policy-driven exit codes."""

import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from trading_system.cli import main
from trading_system.storage.store import Store, canonical

NOW = datetime(2026, 10, 23, 1, tzinfo=UTC)


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr("trading_system.storage.store.utcnow", lambda: NOW)
    root = tmp_path / "catalog"
    with Store(root) as store:
        job = store.start({"code": "86970", "from": "2026-10-01", "to": "2026-10-01"}, "standard")
        body = {
            "data": [
                {
                    "Code": "86970",
                    "Date": "2026-10-01",
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
                }
            ]
        }
        store.save_page(job["job_id"], body, canonical(body).encode(), NOW)
        store.finish(job["job_id"])
    return root / "catalog.duckdb"


def arguments(database):
    return [
        "monitor-prices",
        "--database",
        str(database),
        "--code",
        "8697",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-01",
        "--max-age-hours",
        "24",
        "--observed-at",
        NOW.isoformat(),
    ]


def test_missing_catalog_does_not_create_source_or_output(tmp_path, capsys):
    path = tmp_path / "missing" / "catalog.duckdb"
    output = tmp_path / "report.json"
    assert main(arguments(path) + ["--output", str(output)]) == 2
    assert not path.parent.exists() and not output.exists()
    assert json.loads(capsys.readouterr().out)["message"].startswith("Operation failed")


@pytest.mark.parametrize("name", ["catalog.duckdb", "report.json"])
def test_output_cannot_overwrite_catalog_directory(catalog, capsys, name):
    before = catalog.read_bytes()
    assert main(arguments(catalog) + ["--output", str(catalog.parent / name)]) == 2
    assert catalog.read_bytes() == before
    capsys.readouterr()


def test_process_repetition_and_stale_exit_without_writes(catalog, tmp_path):
    before = catalog.read_bytes()
    command = [sys.executable, "-m", "trading_system.cli", *arguments(catalog)]
    results = [
        subprocess.run(command, capture_output=True, text=True, timeout=30) for _ in range(2)
    ]
    assert [r.returncode for r in results] == [0, 0]
    assert all(not r.stderr for r in results)
    assert json.loads(results[0].stdout) == json.loads(results[1].stdout)
    output = tmp_path / "stale.json"
    stale = subprocess.run(
        command + ["--observed-at", "2026-10-25T01:00:00+00:00", "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert stale.returncode == 3 and not stale.stderr
    assert json.loads(output.read_text())["status"] == "attention"
    assert catalog.read_bytes() == before


@pytest.mark.parametrize("age", ["0", "-1", "nan", "inf"])
def test_invalid_recency_policy_has_no_output(catalog, tmp_path, capsys, age):
    output = tmp_path / "invalid.json"
    assert main(arguments(catalog) + ["--max-age-hours", age, "--output", str(output)]) == 2
    assert not output.exists()
    capsys.readouterr()
