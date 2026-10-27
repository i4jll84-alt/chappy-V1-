"""Opt-in real TOPIX/financial summary checks; licensed responses stay local."""

import json
import os
from datetime import timedelta

import pytest

from trading_system.data_sources.jquants import JQuantsClient
from trading_system.market_financial import (
    MarketFinancialStore,
    ingest_market_financial,
    market_financial_query,
)
from trading_system.settings import Settings, api_key


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("JQUANTS_LIVE_TEST") != "1", reason="Real API test not enabled")
@pytest.mark.parametrize("dataset", ["topix", "financials"])
def test_real_market_financial_repeat(dataset):
    key = api_key()
    assert key is not None, "Configure the API key locally"
    settings = Settings.load()
    params = (
        market_financial_query("topix", start="2026-10-01", end="2026-10-22")
        if dataset == "topix"
        else market_financial_query("financials", code="86970")
    )
    with JQuantsClient(key.get_secret_value(), settings.plan, endpoint=dataset) as client:
        first = ingest_market_financial(
            settings.data_root, client, dataset, params, settings.plan, True
        )
        assert first["state"] == "complete" and first["rows_received"] > 0
        second = ingest_market_financial(
            settings.data_root, client, dataset, params, settings.plan, True
        )
        assert second["state"] == "complete"
        assert first["current_rows"] == second["current_rows"]
        assert first["versions"] == second["versions"], "Provider changed between scans; inspect"
    with MarketFinancialStore(settings.data_root, dataset) as store:
        manifest = json.loads((store.root / "normalized/latest.json").read_bytes())
        file = store.root / manifest["files"][store.current]["path"]
        assert (
            store.db.execute("SELECT count(*) FROM read_parquet(?)", [str(file)]).fetchone()[0] > 0
        )
        completed = store.job(first["job_id"])["completed_at"]
        # The selected first snapshot only becomes eligible after the completion delay.
        before = store.as_of(completed, params)
        if before.num_rows:
            assert all(value <= completed for value in before["available_at"].to_pylist())
            assert first["job_id"] not in before["first_job_id"].to_pylist()
        assert (
            store.as_of(
                store.job(second["job_id"])["completed_at"] + timedelta(seconds=1), params
            ).num_rows
            == second["rows_received"]
        )
