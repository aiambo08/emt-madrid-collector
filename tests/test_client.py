from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from emt_collector.api.client import (
    EMTAuthError,
    EMTClient,
    EMTResponseError,
    EMTTransientError,
    RateLimiter,
)
from tests.conftest import LINES_INFO, LOGIN_OK, FakeClock, arrivals_response, arrive


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def login_headers(self, i: int) -> dict[str, str]:
        return dict(self.requests[i].headers)


def test_login_and_arrivals_send_token(make_client: Callable[..., EMTClient]) -> None:
    rec = Recorder()

    def handler(req: httpx.Request) -> httpx.Response:
        rec.requests.append(req)
        if req.url.path.endswith("/user/login/"):
            assert req.headers["email"] == "user@example.com"
            assert req.headers["password"] == "secret"
            return httpx.Response(200, json=LOGIN_OK)
        assert req.headers["accessToken"] == "tok-1"
        assert req.method == "POST"
        assert req.url.path == "/v2/transport/busemtmad/stops/62/arrives//"
        return httpx.Response(200, json=arrivals_response([arrive("27", "62", 540)]))

    client = make_client(handler)
    resp = client.stop_arrivals("62")
    assert [a.bus for a in resp.arrivals] == [540]
    assert resp.arrivals[0].geometry is not None
    assert resp.arrivals[0].geometry.lat == pytest.approx(40.41)
    assert client.token is not None and client.token.daily_quota == 150000
    assert len(rec.requests) == 2  # login + arrivals


def test_arrivals_with_line_filter_path(make_client: Callable[..., EMTClient]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/user/login/"):
            return httpx.Response(200, json=LOGIN_OK)
        assert req.url.path == "/v2/transport/busemtmad/stops/62/arrives/27/"
        return httpx.Response(200, json=arrivals_response([]))

    client = make_client(handler)
    assert client.stop_arrivals("62", line="27").arrivals == []


def test_reauth_on_expired_token_code(make_client: Callable[..., EMTClient]) -> None:
    logins = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal logins
        if req.url.path.endswith("/user/login/"):
            logins += 1
            body = dict(LOGIN_OK)
            body["data"] = [{**LOGIN_OK["data"][0], "accessToken": f"tok-{logins}"}]
            return httpx.Response(200, json=body)
        if req.headers["accessToken"] == "tok-1":
            return httpx.Response(200, json={"code": "80", "description": "Invalid token"})
        return httpx.Response(200, json=arrivals_response([arrive("27", "62", 1)]))

    client = make_client(handler)
    resp = client.stop_arrivals("62")
    assert len(resp.arrivals) == 1
    assert logins == 2
    assert client.stats.reauths == 1


def test_reauth_on_http_401(make_client: Callable[..., EMTClient]) -> None:
    logins = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal logins
        if req.url.path.endswith("/user/login/"):
            logins += 1
            return httpx.Response(200, json=LOGIN_OK)
        if logins == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=LINES_INFO)

    client = make_client(handler)
    assert [line.label for line in client.list_lines()] == ["27", "45"]
    assert logins == 2


def test_auth_error_surfaces_after_single_reauth(make_client: Callable[..., EMTClient]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/user/login/"):
            return httpx.Response(200, json=LOGIN_OK)
        return httpx.Response(401)

    client = make_client(handler)
    with pytest.raises(EMTAuthError):
        client.stop_arrivals("62")
    assert client.stats.reauths == 1


def test_bad_credentials(make_client: Callable[..., EMTClient]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "98", "description": "Invalid login"})

    client = make_client(handler)
    with pytest.raises(EMTAuthError):
        client.login()


def test_invalid_app_credentials_http_403_code_84(make_client: Callable[..., EMTClient]) -> None:
    """Live API answers HTTP 403 + code 84 (empty description) for a bad X-ClientId/passKey."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "84", "description": "", "data": []})

    client = make_client(handler)
    with pytest.raises(EMTAuthError, match=r"code 84.*EMT_CLIENT_ID / EMT_PASS_KEY"):
        client.login()


def test_user_not_found_code_92_keeps_description(make_client: Callable[..., EMTClient]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "92", "description": "Error: User not found"})

    client = make_client(handler)
    with pytest.raises(EMTAuthError, match=r"code 92.*User not found.*EMT_EMAIL"):
        client.login()


def test_http_4xx_without_json_is_response_error(make_client: Callable[..., EMTClient]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/user/login/"):
            return httpx.Response(200, json=LOGIN_OK)
        return httpx.Response(404, text="not found")

    client = make_client(handler)
    with pytest.raises(EMTResponseError, match="404"):
        client.stop_arrivals("62")


def test_proactive_reauth_when_token_near_expiry(
    make_client: Callable[..., EMTClient], clock: FakeClock
) -> None:
    logins = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal logins
        if req.url.path.endswith("/user/login/"):
            logins += 1
            return httpx.Response(200, json=LOGIN_OK)
        return httpx.Response(200, json=arrivals_response([]))

    client = make_client(handler)
    client.stop_arrivals("1")
    clock.now += 3600 - 30  # inside the 60 s safety margin
    client.stop_arrivals("1")
    assert logins == 2


def test_retries_with_backoff_on_5xx_and_network(
    make_client: Callable[..., EMTClient], clock: FakeClock
) -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if req.url.path.endswith("/user/login/"):
            return httpx.Response(200, json=LOGIN_OK)
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("boom", request=req)
        if attempts == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=arrivals_response([arrive("27", "62", 9)]))

    start = clock.now
    client = make_client(handler)
    resp = client.stop_arrivals("62")
    assert len(resp.arrivals) == 1
    assert attempts == 3
    assert client.stats.retries == 2
    assert clock.now - start >= 1.0  # backoff actually slept


def test_gives_up_after_max_retries(make_client: Callable[..., EMTClient]) -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if req.url.path.endswith("/user/login/"):
            return httpx.Response(200, json=LOGIN_OK)
        attempts += 1
        return httpx.Response(500)

    client = make_client(handler, max_retries=2)
    with pytest.raises(EMTTransientError):
        client.stop_arrivals("62")
    assert attempts == 3


def test_functional_api_error_not_retried(make_client: Callable[..., EMTClient]) -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if req.url.path.endswith("/user/login/"):
            return httpx.Response(200, json=LOGIN_OK)
        attempts += 1
        return httpx.Response(200, json={"code": "90", "description": "Server error"})

    client = make_client(handler)
    with pytest.raises(EMTResponseError) as exc:
        client.stop_arrivals("62")
    assert exc.value.code == "90"
    assert attempts == 1


def test_client_id_passkey_headers(clock: FakeClock) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["X-ClientId"] == "cid"
        assert req.headers["passKey"] == "pk"
        assert "email" not in req.headers
        return httpx.Response(200, json=LOGIN_OK)

    client = EMTClient(
        "https://openapi.test",
        client_id="cid",
        pass_key="pk",
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    assert client.login().value == "tok-1"


def test_rate_limiter_blocks_when_bucket_empty(clock: FakeClock) -> None:
    limiter = RateLimiter(60, clock=clock)
    waited = [limiter.acquire(clock.sleep) for _ in range(60)]
    assert sum(waited) == 0
    extra = limiter.acquire(clock.sleep)
    assert extra == pytest.approx(1.0, abs=0.01)


def test_rate_limiter_applies_to_requests(
    make_client: Callable[..., EMTClient], clock: FakeClock
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/user/login/"):
            return httpx.Response(200, json=LOGIN_OK)
        return httpx.Response(200, json=arrivals_response([]))

    client = make_client(handler, max_requests_per_minute=2)
    start = clock.now
    for i in range(3):  # login + 3 arrivals = 4 requests, bucket of 2 -> 2 waits of 30 s
        client.stop_arrivals(str(i))
    assert clock.now - start == pytest.approx(60.0, abs=0.1)
    assert client.stats.rate_limit_wait_seconds == pytest.approx(60.0, abs=0.1)
