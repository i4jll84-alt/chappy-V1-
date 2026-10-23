"""Small, bounded J-Quants V2 daily-bars client with secret-safe errors."""

from __future__ import annotations

import math
import time
from typing import Callable

import httpx


class APIError(Exception):
    def __init__(self, category: str, status_code: int | None = None):
        self.category = category
        self.status_code = status_code
        super().__init__(
            f"J-Quants request failed: {category}"
            + (f" (HTTP {status_code})" if status_code is not None else "")
        )


class JQuantsClient:
    URL = "https://api.jquants.com/v2/equities/bars/daily"
    LIMITS = {"free": 5, "light": 60, "standard": 120, "premium": 500}

    def __init__(
        self,
        api_key: str,
        plan: str = "standard",
        transport=None,
        sleep: Callable[[float], None] = time.sleep,
        on_attempt: Callable[..., None] | None = None,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("A nonempty API key is required")
        if plan not in self.LIMITS:
            raise ValueError("Unknown J-Quants plan")
        self._interval = 60 / self.LIMITS[plan]
        self._sleep = sleep
        self.on_attempt = on_attempt
        self._last_attempt: float | None = None
        self.last_response_bytes: bytes | None = None
        self._client = httpx.Client(
            headers={"x-api-key": api_key},
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=30,
        )

    def __repr__(self):
        return "JQuantsClient(api_key=<redacted>)"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._client.close()

    def _wait(self, seconds: float):
        while seconds > 0:
            chunk = min(seconds, 60.0)
            self._sleep(chunk)
            seconds -= chunk

    def _record(self, status: int | None, outcome: str):
        if self.on_attempt is not None:
            self.on_attempt(status=status, outcome=outcome)

    def get_page(self, params: dict[str, str]) -> dict:
        self.last_response_bytes = None
        # Every network attempt, including retries, participates in rate limiting.
        for attempt in range(4):
            if self._last_attempt is not None:
                self._wait(max(0, self._interval - (time.monotonic() - self._last_attempt)))
            self._last_attempt = time.monotonic()
            try:
                response = self._client.get(self.URL, params=params)
            except httpx.TransportError:
                self._record(None, "transport_error")
                if attempt == 3:
                    raise APIError("transport_error") from None
                self._wait(2**attempt)
                continue

            status = response.status_code
            if status == 429 or 500 <= status <= 599:
                category = "rate_limited" if status == 429 else "server_error"
                self._record(status, category)
                if attempt == 3:
                    raise APIError(category, status)
                delay = 120.0 if status == 429 else float(2**attempt)
                if status == 429:
                    try:
                        retry_after = float(response.headers.get("Retry-After", "120"))
                        if math.isfinite(retry_after) and retry_after >= 0:
                            delay = max(120.0, retry_after)
                    except ValueError:
                        pass
                self._wait(delay)
                continue
            if status != 200:
                category = {
                    400: "invalid_request_or_period",
                    401: "authentication_failed",
                    403: "access_denied",
                }.get(status, "http_error")
                self._record(status, category)
                raise APIError(category, status)
            try:
                payload = response.json()
            except (ValueError, UnicodeError):
                self._record(status, "malformed_payload")
                raise APIError("malformed_payload", status) from None
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("data"), list)
                or any(not isinstance(row, dict) for row in payload["data"])
                or (
                    "pagination_key" in payload
                    and payload["pagination_key"] is not None
                    and not isinstance(payload["pagination_key"], str)
                )
            ):
                self._record(status, "malformed_payload")
                raise APIError("malformed_payload", status)
            self.last_response_bytes = response.content
            self._record(status, "success")
            return payload
        raise AssertionError("Unreachable")
