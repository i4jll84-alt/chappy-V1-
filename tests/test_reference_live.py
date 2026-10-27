"""Explicitly enabled real reference-data verification, with licensed data kept locally."""

import os

import pytest

from trading_system.data_sources.jquants import JQuantsClient
from trading_system.reference import ingest_reference, reference_query
from trading_system.settings import Settings, api_key


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("JQUANTS_LIVE_TEST") != "1", reason="Real API test not enabled")
@pytest.mark.parametrize("dataset", ["master", "calendar"])
def test_real_reference_repeat(dataset):
    key = api_key()
    assert key is not None, "Configure the API key locally"
    settings = Settings.load()
    params = (
        reference_query("master", date="2026-10-22")
        if dataset == "master"
        else reference_query("calendar", start="2026-10-01", end="2026-10-22")
    )
    with JQuantsClient(key.get_secret_value(), settings.plan, endpoint=dataset) as client:
        first = ingest_reference(settings.data_root, client, dataset, params, settings.plan, True)
        assert first["state"] == "complete" and first["rows_received"] > 0
        second = ingest_reference(settings.data_root, client, dataset, params, settings.plan, True)
        assert second["state"] == "complete"
        assert first["current_rows"] == second["current_rows"]
        assert first["versions"] == second["versions"], "Provider changed between scans; inspect"
        if dataset == "calendar":
            assert first["rows_received"] == 22
