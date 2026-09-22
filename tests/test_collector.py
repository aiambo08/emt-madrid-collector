from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from emt_collector.api.client import EMTClient
from emt_collector.collector import Collector, normalize_line, to_utc
from emt_collector.config import Settings
from emt_collector.db.models import (
    ArrivalEstimate,
    BusPosition,
    CollectionCycle,
    CollectionGap,
    Stop,
)
from emt_collector.db.repository import Repository
from tests.conftest import LINES_INFO, LOGIN_OK, arrivals_response, arrive, line_stops


class FakeAPI:
    """Programmable MockTransport handler for the endpoints the collector uses."""

    def __init__(self) -> None:
        self.arrivals: dict[str, Any] = {}
        self.fail_stops: dict[str, int] = {}
        self.calls: list[str] = []
        self.stops_by_line = {"027": (["62", "63"], ["64"])}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        if path.endswith("/user/login/"):
            return httpx.Response(200, json=LOGIN_OK)
        if "/lines/info/" in path:
            return httpx.Response(200, json=LINES_INFO)
        if "/lines/" in path and "/stops/" in path:
            _, _, _, _, _, line, _, direction, _ = path.split("/")
            stops = self.stops_by_line[line][int(direction) - 1]
            return httpx.Response(200, json=line_stops(line, stops))
        if "/arrives/" in path:
            stop = path.split("/")[5]
            if stop in self.fail_stops:
                return httpx.Response(self.fail_stops[stop])
            return httpx.Response(200, json=self.arrivals.get(stop, arrivals_response([])))
        raise AssertionError(f"unexpected {path}")


def build(
    settings: Settings, repo: Repository, api: FakeAPI, make_client: Callable[..., EMTClient]
) -> Collector:
    return Collector(settings, make_client(api), repo)


def count(engine: Engine, model: Any) -> int:
    with Session(engine) as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_normalize_line() -> None:
    assert normalize_line("001") == "1"
    assert normalize_line(" c1 ") == "C1"
    assert normalize_line("000") == "0"


def test_to_utc_treats_naive_as_madrid() -> None:
    ts = to_utc(datetime(2026, 7, 1, 12, 0, 0), datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert ts == datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)  # CEST = UTC+2


def test_cycle_persists_positions_arrivals_and_cycle_row(
    settings: Settings, repo: Repository, engine: Engine, make_client: Callable[..., EMTClient]
) -> None:
    api = FakeAPI()
    api.arrivals["62"] = arrivals_response(
        [
            arrive("27", "62", 540, eta=71),
            arrive("45", "62", 100),
            arrive("27", "62", 541, eta=999999),
        ]
    )
    api.arrivals["63"] = arrivals_response(
        [arrive("27", "63", 540, eta=200)]
    )  # same bus, other stop
    collector = build(settings, repo, api, make_client)

    result = collector.run_cycle()

    assert result.status == "ok"
    assert result.stops_requested == 3
    assert result.stops_ok == 3
    # line 45 filtered out; bus 540 seen from two stops but stored once per cycle
    assert result.positions_inserted == 2
    assert result.arrivals_inserted == 3
    assert count(engine, Stop) == 3

    with Session(engine) as s:
        cycle = s.scalars(select(CollectionCycle)).one()
        assert cycle.status == "ok"
        assert cycle.finished_at is not None
        assert cycle.api_requests == 1 + 1 + 2 + 3  # login, lines info, 2 directions, 3 stops
        assert cycle.positions_inserted == 2 and cycle.arrivals_inserted == 3
        unknown = s.scalars(select(ArrivalEstimate).where(ArrivalEstimate.bus_id == 541)).one()
        assert unknown.estimate_seconds is None
        pos = s.scalars(select(BusPosition).where(BusPosition.bus_id == 540)).one()
        assert pos.sample_ts.replace(tzinfo=timezone.utc) == datetime(
            2026, 3, 1, 9, 0, tzinfo=timezone.utc
        )
        assert pos.ingested_at is not None
        assert pos.cycle_id == cycle.id
    assert count(engine, CollectionGap) == 0


def test_idempotent_rerun_inserts_nothing(
    settings: Settings, repo: Repository, engine: Engine, make_client: Callable[..., EMTClient]
) -> None:
    api = FakeAPI()
    api.arrivals["62"] = arrivals_response([arrive("27", "62", 540)])
    collector = build(settings, repo, api, make_client)
    first = collector.run_cycle()
    second = collector.run_cycle()  # API returns identical sample timestamps
    assert first.positions_inserted == 1 and first.arrivals_inserted == 1
    assert second.positions_inserted == 0 and second.arrivals_inserted == 0
    assert second.positions_seen == 1
    assert count(engine, BusPosition) == 1
    assert count(engine, ArrivalEstimate) == 1
    assert count(engine, CollectionCycle) == 2


def test_partial_cycle_records_stop_gaps(
    settings: Settings, repo: Repository, engine: Engine, make_client: Callable[..., EMTClient]
) -> None:
    api = FakeAPI()
    api.arrivals["62"] = arrivals_response([arrive("27", "62", 540)])
    api.fail_stops["63"] = 500
    collector = build(settings, repo, api, make_client)
    collector._client._max_retries = 0  # keep the test fast

    result = collector.run_cycle()

    assert result.status == "partial"
    assert result.stops_failed == 1
    with Session(engine) as s:
        gap = s.scalars(select(CollectionGap)).one()
        assert gap.scope == "stop" and gap.stop_id == "63" and gap.kind == "network"
        assert gap.cycle_id == result.cycle_id


def test_all_stops_failed_marks_cycle_failed(
    settings: Settings, repo: Repository, engine: Engine, make_client: Callable[..., EMTClient]
) -> None:
    api = FakeAPI()
    for stop in ("62", "63", "64"):
        api.fail_stops[stop] = 503
    collector = build(settings, repo, api, make_client)
    collector._client._max_retries = 0

    result = collector.run_cycle()

    assert result.status == "failed"
    with Session(engine) as s:
        kinds = {g.kind for g in s.scalars(select(CollectionGap))}
        assert kinds == {"network", "all_stops_failed"}
        assert s.scalars(select(CollectionCycle)).one().status == "failed"


def test_empty_cycle_recorded_as_gap(
    settings: Settings, repo: Repository, engine: Engine, make_client: Callable[..., EMTClient]
) -> None:
    api = FakeAPI()
    collector = build(settings, repo, api, make_client)
    result = collector.run_cycle()
    assert result.status == "empty"
    with Session(engine) as s:
        gap = s.scalars(select(CollectionGap)).one()
        assert gap.scope == "cycle" and gap.kind == "empty"


def test_targets_cached_between_cycles(
    settings: Settings, repo: Repository, make_client: Callable[..., EMTClient]
) -> None:
    api = FakeAPI()
    collector = build(settings, repo, api, make_client)
    collector.run_cycle()
    collector.run_cycle()
    assert sum("/lines/info/" in c for c in api.calls) == 1


def test_explicit_stops_without_lines_polls_only_those(
    repo: Repository, make_client: Callable[..., EMTClient]
) -> None:
    settings = Settings(
        _env_file=None,
        emt_email="u@example.com",
        emt_password="p",
        emt_stops="100, 200",
        emt_max_requests_per_minute=10_000,
    )
    api = FakeAPI()
    api.arrivals["100"] = arrivals_response([arrive("45", "100", 7)])
    collector = build(settings, repo, api, make_client)
    result = collector.run_cycle()
    assert result.stops_requested == 2
    assert result.arrivals_inserted == 1  # no line filter: everything at the stop is kept
    assert not any("/lines/" in c for c in api.calls)


def _settings(**kwargs: Any) -> Settings:
    return Settings(
        _env_file=None,
        emt_email="u@example.com",
        emt_password="p",
        emt_max_requests_per_minute=10_000,
        **kwargs,
    )


def test_explicit_stop_keeps_all_lines_while_line_stops_are_filtered(
    repo: Repository, make_client: Callable[..., EMTClient]
) -> None:
    settings = _settings(emt_lines="27", emt_stops="100, 62")
    api = FakeAPI()
    api.arrivals["100"] = arrivals_response([arrive("45", "100", 7)])
    api.arrivals["62"] = arrivals_response([arrive("27", "62", 540), arrive("45", "62", 8)])
    api.arrivals["63"] = arrivals_response([arrive("27", "63", 541), arrive("45", "63", 9)])
    collector = build(settings, repo, api, make_client)
    result = collector.run_cycle()
    assert result.stops_requested == 4  # 100 + 62/63/64 (62 both explicit and from line 27)
    # stop 100 and 62 are explicit -> every line kept; stop 63 is line-derived -> only 27
    assert result.arrivals_inserted == 1 + 2 + 1


def test_refresh_failure_falls_back_to_cache_and_explicit_stops(
    repo: Repository, engine: Engine, make_client: Callable[..., EMTClient]
) -> None:
    now = datetime.now(timezone.utc)
    repo.upsert_stops(
        [
            {"stop_id": "62", "lines": "27,45", "updated_at": now},
            {"stop_id": "900", "lines": "45", "updated_at": now},  # stale: from another config
        ]
    )
    settings = _settings(emt_lines="27", emt_stops="100")
    api = FakeAPI()
    api.arrivals["62"] = arrivals_response([arrive("27", "62", 540), arrive("45", "62", 8)])
    api.arrivals["100"] = arrivals_response([arrive("45", "100", 7)])

    original = api.__call__

    def failing(req: httpx.Request) -> httpx.Response:
        if "/lines/info/" in req.url.path:
            return httpx.Response(200, json={"code": "90", "description": "maintenance"})
        return original(req)

    collector = Collector(settings, make_client(failing), repo)
    result = collector.run_cycle()
    assert result.status == "ok"
    assert result.stops_requested == 2  # 62 (cached, line 27) + 100 (explicit); 900 excluded
    assert result.arrivals_inserted == 2  # 62 filtered to line 27, 100 keeps line 45
    with Session(engine) as s:
        kinds = set(s.scalars(select(CollectionGap.kind)).all())
    assert "stops_refresh_failed" in kinds
