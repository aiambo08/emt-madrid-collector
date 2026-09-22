from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine

from emt_collector.api.client import EMTClient
from emt_collector.config import Settings
from emt_collector.db.repository import Repository, init_schema

BASE_URL = "https://openapi.test"

LOGIN_OK = {
    "code": "01",
    "description": "Register user",
    "data": [
        {
            "accessToken": "tok-1",
            "tokenSecExpiration": 3600,
            "apiCounter": {"current": 12, "dailyUse": 150000},
        }
    ],
}

LINES_INFO = {
    "code": "00",
    "data": [
        {"line": "027", "label": "27", "nameA": "EMBAJADORES", "nameB": "PLAZA CASTILLA"},
        {"line": "045", "label": "45", "nameA": "REINA VICTORIA", "nameB": "LEGAZPI"},
    ],
}


def line_stops(line: str, stops: list[str]) -> dict[str, Any]:
    return {
        "code": "00",
        "data": [
            {
                "line": line,
                "stops": [
                    {
                        "stop": s,
                        "name": f"Stop {s}",
                        "geometry": {"type": "Point", "coordinates": [-3.7, 40.4]},
                    }
                    for s in stops
                ],
            }
        ],
    }


def arrive(
    line: str, stop: str, bus: int, eta: int = 120, lon: float = -3.69, lat: float = 40.41
) -> dict[str, Any]:
    return {
        "line": line,
        "stop": stop,
        "isHead": "False",
        "destination": "PLAZA CASTILLA",
        "deviation": 0,
        "bus": bus,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "estimateArrive": eta,
        "DistanceBus": 363,
        "positionTypeBus": "0",
    }


def arrivals_response(
    arrives: list[dict[str, Any]], when: str = "2026-03-01T10:00:00.000000"
) -> dict[str, Any]:
    return {
        "code": "00",
        "description": "Data recovered OK",
        "datetime": when,
        "data": [{"Arrive": copy.deepcopy(arrives)}],
    }


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(clock: FakeClock) -> Callable[..., EMTClient]:
    def _make(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> EMTClient:
        return EMTClient(
            BASE_URL,
            email="user@example.com",
            password="secret",
            transport=httpx.MockTransport(handler),
            clock=clock,
            sleep=clock.sleep,
            **kwargs,
        )

    return _make


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    init_schema(eng, use_timescale=False)
    yield eng
    eng.dispose()


@pytest.fixture
def repo(engine: Engine) -> Repository:
    return Repository(engine)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in (
        "EMT_EMAIL",
        "EMT_PASSWORD",
        "EMT_CLIENT_ID",
        "EMT_PASS_KEY",
        "EMT_LINES",
        "EMT_STOPS",
    ):
        monkeypatch.delenv(key, raising=False)
    return Settings(
        _env_file=None,
        emt_email="user@example.com",
        emt_password="secret",
        emt_lines="27",
        database_url="sqlite+pysqlite:///:memory:",
        emt_max_requests_per_minute=10_000,
    )
