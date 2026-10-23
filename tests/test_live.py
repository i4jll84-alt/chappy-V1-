"""Opt-in genuine API verification; never substitutes fixtures for live evidence."""

import json
import os
from pathlib import Path

import pytest

from trading_system.data_sources.jquants import JQuantsClient
from trading_system.ingestion.prices import ingest_prices, price_query
from trading_system.settings import Settings, api_key
from trading_system.storage.store import Store


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("JQUANTS_LIVE_TEST") != "1", reason="Real API test not enabled")
def test_real_prices_parquet_duckdb_and_repeat():
    key = api_key()
    assert key is not None, "Set JQUANTS_API_KEY locally; do not paste it in chat"
    settings = Settings.load()
    params = price_query(
        os.environ.get("JQUANTS_TEST_CODE", "86970"),
        os.environ.get("JQUANTS_TEST_FROM", "2026-10-01"),
        os.environ.get("JQUANTS_TEST_TO", "2026-10-22"),
    )
    # Real licensed data stays under the provider root, never in disposable test fixtures.
    with Store(Path(settings.data_root)) as store:
        with JQuantsClient(key.get_secret_value(), settings.plan) as client:
            first = ingest_prices(store, client, params, settings.plan, refresh=True)
            assert first["state"] == "complete" and first["rows_received"] > 0
            before = store.summary()
            second = ingest_prices(store, client, params, settings.plan, refresh=True)
            assert second["state"] == "complete"
            after = store.summary()
            assert before["current_rows"] == after["current_rows"]
            # A provider correction between the two reads is real evidence requiring review.
            assert before["versions"] == after["versions"], "Source changed during repeat check"
        manifest = json.loads((store.root / "normalized/latest.json").read_text("utf-8"))
        file = store.root / manifest["files"]["prices_current"]["path"]
        assert (
            store.db.execute("SELECT count(*) FROM read_parquet(?)", [str(file)]).fetchone()[0] > 0
        )
        assert (
            store.db.execute("SELECT count(*) FROM attempts WHERE outcome='success'").fetchone()[0]
            >= 2
        )
