import httpx
import pytest

from trading_system.data_sources.jquants import JQuantsClient


@pytest.mark.parametrize(
    "endpoint,path",
    [
        ("prices", "/v2/equities/bars/daily"),
        ("master", "/v2/equities/master"),
        ("calendar", "/v2/markets/calendar"),
    ],
)
def test_fixed_endpoint_routing(endpoint, path):
    def handler(request):
        assert request.url.host == "api.jquants.com"
        assert request.url.path == path
        assert request.headers["x-api-key"] == "test-secret"
        return httpx.Response(200, json={"data": []})

    with JQuantsClient(
        "test-secret", endpoint=endpoint, transport=httpx.MockTransport(handler)
    ) as client:
        assert client.get_page({}) == {"data": []}


def test_reject_arbitrary_endpoint():
    with pytest.raises(ValueError, match="Unsupported"):
        JQuantsClient("test-secret", endpoint="https://example.com/")
