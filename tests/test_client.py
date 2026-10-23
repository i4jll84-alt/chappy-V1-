import httpx
import pytest

from trading_system.data_sources.jquants import APIError, JQuantsClient


def client(handler, **kwargs):
    return JQuantsClient(
        "secret-key",
        transport=httpx.MockTransport(handler),
        sleep=kwargs.pop("sleep", lambda _: None),
        **kwargs,
    )


def test_page_and_pagination_preserved():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json={"data": [{"Code": "186A0", "unknown": 7}], "pagination_key": "next"}
        )

    with client(handler) as api:
        page = api.get_page({"code": "186A0", "pagination_key": "first"})
    assert page["pagination_key"] == "next"
    assert page["data"][0]["unknown"] == 7
    assert str(seen[0].url).startswith(JQuantsClient.URL)
    assert seen[0].url.params["pagination_key"] == "first"
    assert seen[0].headers["x-api-key"] == "secret-key"
    assert seen[0].extensions["timeout"]["read"] == 30


def test_429_waits_at_least_120_in_bounded_chunks():
    waits, attempts = [], []

    def handler(request):
        attempts.append(request)
        return httpx.Response(429 if len(attempts) == 1 else 200, json={"data": []})

    with client(handler, sleep=waits.append) as api:
        assert api.get_page({}) == {"data": []}
    assert sum(waits) >= 120
    assert all(0 < value <= 60 for value in waits)
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "header,minimum",
    [("0", 120), ("1", 120), ("180", 180), ("nan", 120), ("-1", 120), ("bad", 120)],
)
def test_retry_after_has_minimum_delay(header, minimum):
    waits, attempts = [], []

    def handler(request):
        attempts.append(request)
        return httpx.Response(
            429 if len(attempts) == 1 else 200, headers={"Retry-After": header}, json={"data": []}
        )

    with client(handler, sleep=waits.append) as api:
        api.get_page({})
    assert sum(waits) >= minimum
    assert all(0 < value <= 60 for value in waits)


def test_response_bytes_preserve_json_formatting():
    raw = b'{ "data" : [ { "Code": "186A0", "Vo": 1.00 } ], "extra": "\\u65e5" }\n'
    with client(lambda _: httpx.Response(200, content=raw)) as api:
        assert api.last_response_bytes is None
        assert api.get_page({})["data"][0]["Vo"] == 1
        assert api.last_response_bytes == raw


def test_failed_page_does_not_leave_stale_response_bytes():
    responses = iter(
        [httpx.Response(200, content=b'{"data":[]}'), httpx.Response(200, content=b"not json")]
    )
    with client(lambda _: next(responses)) as api:
        api.get_page({})
        with pytest.raises(APIError):
            api.get_page({})
        assert api.last_response_bytes is None


@pytest.mark.parametrize(
    "status,category",
    [
        (400, "invalid_request_or_period"),
        (401, "authentication_failed"),
        (403, "access_denied"),
        (302, "http_error"),
    ],
)
def test_no_retry_and_no_secrets(status, category):
    events = []

    def handler(request):
        return httpx.Response(
            status, text="secret-key PRIVATE-BODY", headers={"Location": "https://example.com"}
        )

    with client(handler, on_attempt=lambda **event: events.append(event)) as api:
        with pytest.raises(APIError) as caught:
            api.get_page({})
        assert "secret-key" not in repr(api)
    assert caught.value.status_code == status
    assert caught.value.category == category
    assert len(events) == 1
    assert "secret-key" not in str(caught.value)
    assert "PRIVATE-BODY" not in repr(caught.value)
    assert events == [{"status": status, "outcome": category}]


def test_timeout_retries_and_sanitizes():
    events = []

    def handler(request):
        raise httpx.ReadTimeout("secret-key PRIVATE-BODY", request=request)

    with client(handler, on_attempt=lambda **event: events.append(event)) as api:
        with pytest.raises(APIError) as caught:
            api.get_page({})
    assert len(events) == 4
    assert caught.value.category == "transport_error"
    assert caught.value.status_code is None
    assert caught.value.__suppress_context__
    assert "secret-key" not in str(caught.value)


@pytest.mark.parametrize(
    "body", [[], {}, {"data": {}}, {"data": [1]}, {"data": [], "pagination_key": 1}]
)
def test_bad_payload_shape(body):
    with client(lambda _: httpx.Response(200, json=body)) as api:
        with pytest.raises(APIError, match="malformed_payload"):
            api.get_page({})


def test_bad_json():
    with client(lambda _: httpx.Response(200, text="secret-key")) as api:
        with pytest.raises(APIError, match="malformed_payload") as caught:
            api.get_page({})
    assert "secret-key" not in str(caught.value)


def test_server_retry_limit():
    events = []
    with client(
        lambda _: httpx.Response(503), on_attempt=lambda **event: events.append(event)
    ) as api:
        with pytest.raises(APIError) as caught:
            api.get_page({})
    assert len(events) == 4
    assert caught.value.status_code == 503


@pytest.mark.parametrize(
    "plan,minimum", [("free", 12), ("light", 1), ("standard", 0.5), ("premium", 0.12)]
)
def test_plan_rate_limit(plan, minimum, monkeypatch):
    waits = []
    monkeypatch.setattr("trading_system.data_sources.jquants.time.monotonic", lambda: 10)
    with client(
        lambda _: httpx.Response(200, json={"data": []}), plan=plan, sleep=waits.append
    ) as api:
        api.get_page({})
        api.get_page({})
    assert sum(waits) == pytest.approx(minimum)
