"""Bounded read-only kabuStation transport. Credentials never leave process memory."""

import re
from datetime import UTC, datetime

import httpx

from trading_system.brokers.mufg_esmart.connection_check import (
    BASE,
    BOARD_PATH,
    ProbeError,
    ReadOnlyTransport,
    _contains_secret,
    _object,
    _read,
)


class Client:
    """Only token POST and one fixed-symbol board GET; retries belong to the runner."""

    def __init__(self, password, *, transport_factory=None, clock=None):
        if not isinstance(password, str) or not re.fullmatch(r"[A-Za-z0-9]{6,16}", password):
            raise ValueError("api_password_format_invalid")
        self._password = password
        self._token = None
        self._factory = transport_factory or (lambda: httpx.HTTPTransport(retries=0))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._http = None
        self._reauths = 0
        self.closed = False

    @property
    def authenticated(self):
        return self._token is not None

    def reconnect(self):
        if self._http is not None:
            self._http.close()
        self._http = None

    def read(self):
        if self.closed:
            raise ValueError("client_closed")
        operation = "board" if self.authenticated else "token"
        result = {
            "broker": "mufg_esmart",
            "code": "7203",
            "operation": operation,
            "read_started_at": self._clock().isoformat(),
            "http_status": None,
            "outcome": "error",
            "reason": None,
            "retryable": False,
            "source_updated_at": None,
            "published_at": None,
            "freshness_verified": False,
            "real_execution_enabled": False,
            "order_requests": 0,
        }
        raw = None
        try:
            if self._http is None:
                self._http = httpx.Client(
                    transport=ReadOnlyTransport(self._factory()),
                    timeout=5,
                    trust_env=False,
                    follow_redirects=False,
                )
            if operation == "token":
                status, body = _read(
                    self._http, "POST", BASE + "/token", json={"APIPassword": self._password}
                )
            else:
                status, body = _read(
                    self._http, "GET", BASE + BOARD_PATH, headers={"X-API-KEY": self._token}
                )
            result["http_status"] = status
            if operation == "board":
                if _contains_secret(body, (self._password, self._token)):
                    raise ProbeError("sensitive_response_not_saved")
                raw = body
            if status == 200:
                payload = _object(body)
                if operation == "token":
                    token = payload.get("Token")
                    if (
                        type(payload.get("ResultCode")) is not int
                        or payload["ResultCode"] != 0
                        or not isinstance(token, str)
                        or not re.fullmatch(r"[\x21-\x7e]{1,4096}", token)
                    ):
                        raise ProbeError("invalid_token_response")
                    self._token = token
                result["outcome"] = "ok"
            elif operation == "board" and status in (401, 403) and self._reauths == 0:
                self._reauths += 1
                self._token = None
                result.update(reason="token_expired", retryable=True)
            elif status in (429, 500, 503):
                result.update(reason=f"{operation}_http_{status}", retryable=True)
            else:
                result.update(
                    reason=f"{operation}_http_{status}",
                    outcome="auth_required" if status in (400, 401, 403) else "error",
                )
        except ProbeError as exc:
            result["reason"] = str(exc)
        except httpx.HTTPError:
            result.update(reason="network_request_failed", retryable=True)
            self.reconnect()
        result["read_completed_at"] = self._clock().isoformat()
        return result, raw

    def close(self):
        self.reconnect()
        self._password = self._token = None
        self.closed = True


class RecoveryProbe:
    """Opt-in, process-local fault injection. Never disables the PC or broker app."""

    def __init__(self, *, after=4, failures=2):
        self.after, self.remaining = after, failures
        self.successes = 0
        self.injected = 0
        self.just_injected = False

    def transport(self):
        probe = self

        class Transport(httpx.BaseTransport):
            def __init__(self):
                self.inner = httpx.HTTPTransport(retries=0)

            def handle_request(self, request):
                probe.just_injected = False
                if request.method == "GET" and probe.successes >= probe.after and probe.remaining:
                    probe.remaining -= 1
                    probe.injected += 1
                    probe.just_injected = True
                    self.inner.close()
                    raise httpx.ConnectError("controlled_local_transport_interruption")
                response = self.inner.handle_request(request)
                if request.method == "GET" and response.status_code == 200:
                    probe.successes += 1
                return response

            def close(self):
                self.inner.close()

        return Transport()
