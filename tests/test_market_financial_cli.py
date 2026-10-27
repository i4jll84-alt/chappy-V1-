import json

import httpx
import pytest
from pydantic import SecretStr

from trading_system import cli
from trading_system.data_sources.jquants import JQuantsClient


@pytest.mark.parametrize(
    "command,flags,dataset,params",
    [
        (
            "fetch-topix",
            ["--from", "2026-10-01", "--to", "2026-10-22"],
            "topix",
            {"from": "2026-10-01", "to": "2026-10-22"},
        ),
        ("fetch-financials", ["--code", "8697"], "financials", {"code": "86970"}),
        ("fetch-financials", ["--date", "2026-10-22"], "financials", {"date": "2026-10-22"}),
    ],
)
def test_cli_routes(monkeypatch, tmp_path, capsys, command, flags, dataset, params):
    monkeypatch.setattr(cli, "api_key", lambda: SecretStr("test-secret"))

    def acquire(root, client, actual_dataset, actual_params, plan, refresh):
        assert root == tmp_path and client.endpoint == actual_dataset == dataset
        assert actual_params == params and plan == "standard" and refresh
        return {"state": "complete", "rows_received": 1}

    monkeypatch.setattr(cli, "ingest_market_financial", acquire)
    assert cli.main(["--data-root", str(tmp_path), command, *flags, "--refresh"]) == 0
    output = capsys.readouterr().out
    assert "test-secret" not in output and json.loads(output)["state"] == "complete"
    assert not (tmp_path / "catalog.duckdb").exists()


@pytest.mark.parametrize(
    "command,flags",
    [
        ("fetch-topix", ["--from", "2026-10-01", "--to", "2026-10-22"]),
        ("fetch-financials", ["--code", "8697"]),
    ],
)
def test_missing_key(monkeypatch, capsys, command, flags):
    monkeypatch.setattr(cli, "api_key", lambda: None)
    assert cli.main([command, *flags]) == 2
    assert "No API call" in capsys.readouterr().out


def test_financials_requires_code_or_date_before_http(monkeypatch, capsys):
    monkeypatch.setattr(cli, "api_key", lambda: SecretStr("test-secret"))
    monkeypatch.setattr(
        cli, "JQuantsClient", lambda *a, **k: pytest.fail("Invalid query reached HTTP")
    )
    assert cli.main(["fetch-financials"]) == 2
    assert "test-secret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "endpoint,path", [("topix", "/v2/indices/bars/daily/topix"), ("financials", "/v2/fins/summary")]
)
def test_fixed_endpoint(endpoint, path):
    def handler(request):
        assert request.url.host == "api.jquants.com" and request.url.path == path
        assert request.headers["x-api-key"] == "test-secret"
        return httpx.Response(200, json={"data": []})

    with JQuantsClient(
        "test-secret", endpoint=endpoint, transport=httpx.MockTransport(handler)
    ) as client:
        client.get_page({})


@pytest.mark.parametrize("plan", ["free", "light", "standard", "premium"])
def test_financials_own_60rpm_limit(plan, monkeypatch):
    waits = []
    monkeypatch.setattr("trading_system.data_sources.jquants.time.monotonic", lambda: 10)
    with JQuantsClient(
        "test-secret",
        endpoint="financials",
        plan=plan,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []})),
        sleep=waits.append,
    ) as client:
        client.get_page({})
        client.get_page({})
    assert sum(waits) == pytest.approx(1.0)
