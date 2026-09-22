from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx
import structlog
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from emt_collector.api.models import ArrivalsResponse, LineInfo, LineStops, StopInfo

log = structlog.get_logger(__name__)

LOGIN_PATH = "/v1/mobilitylabs/user/login/"
LINES_INFO_PATH = "/v2/transport/busemtmad/lines/info/{dateref}/"
LINE_STOPS_PATH = "/v1/transport/busemtmad/lines/{line}/stops/{direction}/"
STOP_ARRIVES_PATH = "/v2/transport/busemtmad/stops/{stop}/arrives/{line}/"

OK_CODES = {"00", "01"}
# Codes MobilityLabs returns for a missing/expired/invalid accessToken.
AUTH_ERROR_CODES = {"80", "81", "82", "83", "84", "85", "86", "87", "88", "89"}
# Login-time codes observed against the live API (descriptions are often empty).
LOGIN_ERROR_HINTS = {
    "84": "invalid X-ClientId/passKey (EMT_CLIENT_ID / EMT_PASS_KEY)",
    "92": "user not found or wrong password (EMT_EMAIL / EMT_PASSWORD)",
    "99": "no credentials received by the API",
}

ARRIVES_BODY = {
    "cultureInfo": "ES",
    "Text_StopRequired_YN": "N",
    "Text_EstimationsRequired_YN": "Y",
    "Text_IncidencesRequired_YN": "N",
}


class EMTError(Exception):
    """Base error for the EMT client."""


class EMTTransientError(EMTError):
    """Network failure or 5xx: safe to retry."""


class EMTAuthError(EMTError):
    """Token missing/expired/invalid, or bad credentials."""


class EMTResponseError(EMTError):
    """Non-retryable functional error reported by the API."""

    def __init__(self, code: str, description: str) -> None:
        super().__init__(f"EMT API code {code}: {description}")
        self.code = code
        self.description = description


class RateLimiter:
    """Token bucket: at most `rate_per_minute` acquisitions per rolling minute."""

    def __init__(self, rate_per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be > 0")
        self._capacity = float(rate_per_minute)
        self._refill_per_sec = rate_per_minute / 60.0
        self._tokens = self._capacity
        self._clock = clock
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self, sleep: Callable[[float], None] = time.sleep) -> float:
        """Block until a token is available. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._last) * self._refill_per_sec
                )
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return waited
                delay = (1 - self._tokens) / self._refill_per_sec
            sleep(delay)
            waited += delay


@dataclass
class Token:
    value: str
    expires_at: float
    daily_quota: int | None = None
    used_today: int | None = None

    def is_expired(self, now: float, margin: float = 60.0) -> bool:
        return now >= self.expires_at - margin


@dataclass
class ClientStats:
    requests: int = 0
    retries: int = 0
    reauths: int = 0
    rate_limit_wait_seconds: float = 0.0
    by_status: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "reauths": self.reauths,
            "rate_limit_wait_seconds": round(self.rate_limit_wait_seconds, 2),
        }


class EMTClient:
    """Thin, synchronous client for the EMT MobilityLabs OpenAPI.

    Handles login, transparent re-authentication on token expiry, exponential backoff on
    network/5xx errors and client-side rate limiting.
    """

    def __init__(
        self,
        base_url: str,
        *,
        email: str | None = None,
        password: str | None = None,
        client_id: str | None = None,
        pass_key: str | None = None,
        max_requests_per_minute: int = 100,
        timeout: float = 15.0,
        max_retries: int = 4,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not ((email and password) or (client_id and pass_key)):
            raise ValueError("Provide email+password or client_id+pass_key")
        self._email = email
        self._password = password
        self._client_id = client_id
        self._pass_key = pass_key
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )
        self._limiter = RateLimiter(max_requests_per_minute, clock=clock)
        self._clock = clock
        self._sleep = sleep
        self._token: Token | None = None
        self._token_lock = threading.Lock()
        self.stats = ClientStats()
        self._max_retries = max_retries

    # -- lifecycle -------------------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EMTClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth ------------------------------------------------------------------------------

    @property
    def token(self) -> Token | None:
        return self._token

    def _login_headers(self) -> dict[str, str]:
        if self._client_id and self._pass_key:
            return {"X-ClientId": self._client_id, "passKey": self._pass_key}
        assert self._email and self._password
        return {"email": self._email, "password": self._password}

    def login(self) -> Token:
        with self._token_lock:
            payload = self._request_raw(
                "GET", LOGIN_PATH, headers=self._login_headers(), auth=False
            )
            code = str(payload.get("code", ""))
            if code not in OK_CODES:
                raise EMTAuthError(_login_failure(code, str(payload.get("description") or "")))
            try:
                data = payload["data"][0]
                value = str(data["accessToken"])
            except (KeyError, IndexError, TypeError) as exc:
                raise EMTAuthError("login response without accessToken") from exc
            ttl = float(data.get("tokenSecExpiration") or 3600)
            counter = data.get("apiCounter") or {}
            token = Token(
                value=value,
                expires_at=self._clock() + ttl,
                daily_quota=_as_int(counter.get("dailyUse")),
                used_today=_as_int(counter.get("current")),
            )
            self._token = token
            log.info(
                "emt.login",
                token_ttl_seconds=int(ttl),
                daily_quota=token.daily_quota,
                used_today=token.used_today,
            )
            return token

    def _ensure_token(self) -> Token:
        token = self._token
        if token is None or token.is_expired(self._clock()):
            if token is not None:
                self.stats.reauths += 1
                log.info("emt.token_expired_proactive_reauth")
            return self.login()
        return token

    # -- low-level request -----------------------------------------------------------------

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """Perform a request with rate limiting and retries. Returns the parsed JSON body."""

        def _on_retry(state: RetryCallState) -> None:
            self.stats.retries += 1
            exc = state.outcome.exception() if state.outcome else None
            log.warning(
                "emt.retry",
                path=path,
                attempt=state.attempt_number,
                error=str(exc),
                sleep=round(state.next_action.sleep, 2) if state.next_action else None,
            )

        @retry(
            reraise=True,
            retry=retry_if_exception_type(EMTTransientError),
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential_jitter(initial=1, max=30),
            before_sleep=_on_retry,
            sleep=self._sleep,
        )
        def _do() -> dict[str, Any]:
            hdrs = dict(headers or {})
            if auth:
                hdrs["accessToken"] = self._ensure_token().value
            self.stats.rate_limit_wait_seconds += self._limiter.acquire(self._sleep)
            self.stats.requests += 1
            try:
                resp = self._http.request(method, path, headers=hdrs, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                raise EMTTransientError(f"{type(exc).__name__}: {exc}") from exc
            key = str(resp.status_code)
            self.stats.by_status[key] = self.stats.by_status.get(key, 0) + 1
            if resp.status_code == 401:
                raise EMTAuthError("HTTP 401")
            if resp.status_code == 429 or resp.status_code >= 500:
                raise EMTTransientError(f"HTTP {resp.status_code}")
            try:
                body = resp.json()
            except ValueError as exc:
                if resp.status_code >= 400:
                    raise EMTResponseError(str(resp.status_code), resp.text[:200]) from exc
                raise EMTTransientError("non-JSON body") from exc
            if not isinstance(body, dict):
                raise EMTResponseError(str(resp.status_code), "unexpected JSON shape")
            code = str(body.get("code", ""))
            description = str(body.get("description") or "")
            if code in AUTH_ERROR_CODES:
                if not auth:
                    raise EMTAuthError(_login_failure(code, description))
                raise EMTAuthError(f"code {code}: {description}")
            if resp.status_code >= 400:
                raise EMTResponseError(code or str(resp.status_code), description)
            return body

        return _do()

    def _request(self, method: str, path: str, *, json: Any | None = None) -> dict[str, Any]:
        """Authenticated request; re-authenticates once on token errors."""
        try:
            return self._request_raw(method, path, json=json)
        except EMTAuthError as exc:
            log.warning("emt.reauth", reason=str(exc), path=path)
            self.stats.reauths += 1
            self._token = None
            self.login()
            return self._request_raw(method, path, json=json)

    # -- endpoints -------------------------------------------------------------------------

    def list_lines(self, date_ref: date | None = None) -> list[LineInfo]:
        dateref = (date_ref or datetime.now().date()).strftime("%Y%m%d")
        body = self._request("GET", LINES_INFO_PATH.format(dateref=dateref))
        _raise_for_code(body)
        return [LineInfo.model_validate(item) for item in body.get("data") or []]

    def line_stops(self, line: str, direction: int) -> LineStops:
        if direction not in (1, 2):
            raise ValueError("direction must be 1 or 2")
        body = self._request("GET", LINE_STOPS_PATH.format(line=line, direction=direction))
        _raise_for_code(body)
        data = body.get("data") or []
        if not data:
            return LineStops(line=line, stops=[])
        item = data[0]
        stops = [StopInfo.model_validate(s) for s in item.get("stops") or []]
        return LineStops(line=str(item.get("line", line)), stops=stops)

    def stop_arrivals(self, stop: str, line: str | None = None) -> ArrivalsResponse:
        path = STOP_ARRIVES_PATH.format(stop=stop, line=line or "")
        body = self._request("POST", path, json=ARRIVES_BODY)
        _raise_for_code(body)
        return ArrivalsResponse.model_validate(body)


def _login_failure(code: str, description: str) -> str:
    hint = LOGIN_ERROR_HINTS.get(code)
    detail = description or hint or ""
    if description and hint:
        detail = f"{description} ({hint})"
    return f"login failed (code {code}): {detail}"


def _raise_for_code(body: dict[str, Any]) -> None:
    code = str(body.get("code", ""))
    if code not in OK_CODES:
        raise EMTResponseError(code, str(body.get("description", "")))


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
