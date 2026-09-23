from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from emt_collector.__main__ import main
from emt_collector.analysis.stats import history_stats
from emt_collector.db.models import ArrivalEstimate, BusPosition, CollectionCycle, CollectionGap

START = datetime(2026, 1, 12, 8, tzinfo=timezone.utc)


def at(minute: int) -> datetime:
    return START + timedelta(minutes=minute)


def _populate(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        for minute in range(0, 120):
            session.add(
                CollectionCycle(
                    started_at=at(minute),
                    finished_at=at(minute) + timedelta(seconds=3),
                    status="ok" if minute % 40 else "partial",
                    stops_failed=0 if minute % 40 else 1,
                    api_requests=5,
                )
            )
        session.add(CollectionGap(occurred_at=at(40), scope="stop", kind="network", stop_id="100"))
        # bus 1 reaches the stop at minute 10 (near), bus 2 vanishes at minute 30 with ETA 90 s,
        # bus 3 stays far away and bus 1 comes back at minute 70 (a 60-minute cycle).
        rows = [(10, 1, 0, 0), (29, 2, 150, 900), (30, 2, 90, 500), (70, 1, 0, 0)]
        rows += [(minute, 3, 3600, 9000) for minute in range(0, 120)]
        for minute, bus, eta, distance in rows:
            session.add(
                ArrivalEstimate(
                    stop_id="100",
                    line="27",
                    bus_id=bus,
                    sample_ts=at(minute),
                    ingested_at=at(minute),
                    estimate_seconds=eta,
                    distance_m=distance,
                    destination="NORTE",
                )
            )
        session.add(
            BusPosition(line="27", bus_id=1, sample_ts=at(10), ingested_at=at(10), lat=40, lon=-3)
        )


def test_history_stats_reports_coverage_passages_and_hints(engine: Engine) -> None:
    _populate(engine)
    report = history_stats(engine, at(0), at(121), interval_seconds=60, quota=20000)
    history, cycles = report["history"], report["cycles"]
    assert isinstance(history, dict) and isinstance(cycles, dict)
    assert history["arrivals"] == 124 and history["positions"] == 1
    assert history["stops"] == history["lines"] == 1 and history["buses"] == 3
    assert cycles["total"] == 120 and cycles["by_status"] == {"ok": 117, "partial": 3}
    assert cycles["stops_failed"] == 3 and cycles["gaps_by_kind"] == {"network": 1}
    assert cycles["expected_per_day"] == 1440.0
    routes = report["routes"]
    assert isinstance(routes, list) and len(routes) == 1
    route = routes[0]
    assert (route["line"], route["stop_id"], route["destination"]) == ("27", "100", "NORTE")
    assert route["passages"] == 3 and route["vanish_passages"] == 1
    assert route["cycle_returns"] == 1 and route["median_cycle_minutes"] == 60.0
    hints = report["hints"]
    assert isinstance(hints, list)
    assert any("días de histórico" in hint for hint in hints)
    assert any("pasos/día" in hint for hint in hints)
    assert not any("peticiones/día" in hint for hint in hints)


def test_history_stats_on_empty_database(engine: Engine) -> None:
    report = history_stats(engine, at(0), at(1), interval_seconds=60, quota=20000)
    assert report["routes"] == []
    assert report["hints"] == [
        "Sin llegadas en el periodo: comprueba que el recolector está en marcha."
    ]


def test_stats_command_needs_no_credentials(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in ("EMT_EMAIL", "EMT_PASSWORD", "EMT_CLIENT_ID", "EMT_PASS_KEY", "EMT_LINES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr("emt_collector.__main__.make_engine", lambda _: engine)
    assert main(["stats", "--days", "1"]) == 3
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{\n") :])
    assert report["hints"][0].startswith("Sin llegadas")
