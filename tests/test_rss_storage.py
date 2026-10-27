import json
from datetime import UTC, datetime

import duckdb
import pyarrow.parquet as pq
import pytest

from trading_system.data_sources.rss_live import observe
from trading_system.data_sources.rss_snapshot import ITEMS
from trading_system.storage import rss


def make_run(tmp_path, price=100):
    formulas = [f'=RssMarket("1234.T","{item}")' for item in ITEMS.values()]
    raw = dict(
        schema_version=1,
        status="read",
        formulas_before=formulas,
        formulas_after=formulas,
        values_before=[price, "15:30:00", "2026/01/05"],
        values_after=[price, "15:30:00", "2026/01/05"],
    )
    run, _ = observe(
        tmp_path / "unused.xlsx",
        "1234.T",
        tmp_path / "runs",
        samples=1,
        reader=lambda *_: raw,
        utcnow=lambda: datetime(2026, 1, 5, 8, tzinfo=UTC),
    )
    return run


def test_import_repeat_and_parquet_rebuild(tmp_path):
    run = make_run(tmp_path)
    original = (run / "000.json").read_bytes()
    with rss.RSSStore(tmp_path / "store") as store:
        assert store.import_run(run) == dict(imported=1, unchanged=0, attention=0)
        assert store.import_run(run) == dict(imported=0, unchanged=1, attention=0)
        row = store.db.execute(
            "SELECT price,quote_date,source_updated_at,freshness_verified,"
            "available_at > observed_at FROM observations"
        ).fetchone()
        assert row == ("100", None, None, False, True)
        first = store.export()
        path = store.root / "parquet" / first["generation"] / "observations.parquet"
        with path.open("rb") as handle:
            table = pq.read_table(handle)
        path.unlink()
        second = store.export()
        assert pq.read_table(
            store.root / "parquet" / second["generation"] / "observations.parquet"
        ).equals(table)
        assert len(list((store.root / "raw").glob("*.json"))) == 1
    assert (run / "000.json").read_bytes() == original


def test_changed_identity_keeps_both_raws_and_original_row(tmp_path):
    run = make_run(tmp_path)
    with rss.RSSStore(tmp_path / "store") as store:
        store.import_run(run)
        path = run / "000.json"
        path.write_bytes(path.read_bytes() + b" ")
        with pytest.raises(ValueError, match="identity_conflict"):
            store.import_run(run)
        assert store.db.execute("SELECT count(*) FROM observations").fetchone() == (1,)
        assert len(list((store.root / "raw").glob("*.json"))) == 2


@pytest.mark.parametrize("mutation", ["json", "future", "naive", "raw", "price"])
def test_invalid_records_preserved_and_flagged(tmp_path, mutation):
    run = make_run(tmp_path)
    path = run / "000.json"
    record = json.loads(path.read_bytes())
    if mutation == "future":
        record["read_completed_at"] = "2099-01-01T00:00:00+00:00"
    elif mutation == "naive":
        record["read_started_at"] = "2026-01-05T08:00:00"
    elif mutation == "raw":
        record["raw"] = []
    elif mutation == "price":
        record["raw"]["values_before"][0] = -1
        record["raw"]["values_after"][0] = -1
    path.write_bytes(b"broken" if mutation == "json" else json.dumps(record).encode())
    with rss.RSSStore(tmp_path / "store") as store:
        assert store.import_run(run)["attention"] == 1
        assert store.import_run(run) == dict(imported=0, unchanged=1, attention=1)
        assert store.db.execute("SELECT price FROM observations").fetchone() == (None,)
        assert next((store.root / "raw").glob("*.json")).read_bytes() == path.read_bytes()


def test_failed_checkpoint_rolls_back_observation_and_retries(tmp_path):
    run = make_run(tmp_path)
    with rss.RSSStore(tmp_path / "store") as store:
        store.db.execute("ALTER TABLE imports RENAME TO original_imports")
        store.db.execute("CREATE TABLE imports AS SELECT * FROM original_imports")
        store.db.execute("ALTER TABLE imports ADD COLUMN fail INTEGER DEFAULT 1")
        with pytest.raises(duckdb.Error):
            store.import_run(run)
        assert store.db.execute("SELECT count(*) FROM observations").fetchone() == (0,)
        store.db.execute("DROP TABLE imports")
        store.db.execute("ALTER TABLE original_imports RENAME TO imports")
        assert store.import_run(run)["imported"] == 1


def test_failed_export_does_not_publish_partial_generation(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    with rss.RSSStore(tmp_path / "store") as store:
        store.import_run(run)
        store.export()
        manifest = store.root / "parquet" / "current.json"
        before = manifest.read_bytes()

        def fail(*args, **kwargs):
            raise OSError("simulated")

        monkeypatch.setattr(rss.pq, "write_table", fail)
        with pytest.raises(OSError):
            store.export()
        assert manifest.read_bytes() == before


def test_tampered_archive_is_not_overwritten(tmp_path):
    run = make_run(tmp_path)
    with rss.RSSStore(tmp_path / "store") as store:
        store.import_run(run)
        archive = next((store.root / "raw").glob("*.json"))
        archive.write_bytes(b"tampered")
        with pytest.raises(ValueError, match="archive_mismatch"):
            store.import_run(run)
        assert archive.read_bytes() == b"tampered"


def test_separate_observations_with_same_price_remain_separate(tmp_path):
    run = make_run(tmp_path)
    (run / "001.json").write_bytes((run / "000.json").read_bytes())
    with rss.RSSStore(tmp_path / "store") as store:
        assert store.import_run(run)["imported"] == 2
        assert len(list((store.root / "raw").glob("*.json"))) == 1
