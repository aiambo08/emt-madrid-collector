from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from emt_collector.api.client import EMTClient
from emt_collector.bunching.data import load_history
from emt_collector.bunching.detector import build_series
from emt_collector.bunching.domain import Parameters, Route, utc
from emt_collector.bunching.report import Prediction as BunchingPrediction
from emt_collector.bunching.report import predict as predict_bunching
from emt_collector.collector import normalize_line
from emt_collector.db.models import CollectionCycle, CollectionGap, Stop
from emt_collector.saturation.report import Prediction as SaturationPrediction
from emt_collector.saturation.report import predict as predict_saturation
from emt_collector.telegram.models import Loaded, ModelStore


@dataclass(frozen=True)
class Arrival:
    line: str
    destination: str
    bus_id: int
    eta_seconds: int | None
    distance_m: int | None


@dataclass(frozen=True)
class Risk:
    route: Route
    bunching_probability: float | None
    bunching_status: str | None
    horizon_minutes: int | None
    saturation_probability: float | None
    saturation_status: str | None
    expected_wait_minutes: float | None
    minutes_since_last_bus: float | None
    threshold_minutes: float | None

    @property
    def usable(self) -> bool:
        return self.bunching_status == "ok" or self.saturation_status == "ok"


@dataclass(frozen=True)
class CycleInfo:
    started_at: datetime
    status: str
    stops_ok: int
    stops_failed: int
    arrivals_inserted: int


@dataclass(frozen=True)
class Status:
    now: datetime
    last_cycle: CycleInfo | None
    cycles_last_hour: int
    gaps_last_hour: int
    arrivals_last_hour: int
    models: Loaded


class DataSource(Protocol):
    def arrivals(self, stop: str, line: str | None) -> list[Arrival]: ...

    def stop_name(self, stop: str) -> str | None: ...

    def risks(self, stops: list[str] | None, at: datetime) -> list[Risk]: ...

    def status(self, at: datetime) -> Status: ...

    def models(self) -> Loaded: ...


class LiveDataSource:
    def __init__(self, client: EMTClient, engine: Engine, models: ModelStore) -> None:
        self._client = client
        self._engine = engine
        self._store = models

    def models(self) -> Loaded:
        return self._store.current

    def arrivals(self, stop: str, line: str | None) -> list[Arrival]:
        response = self._client.stop_arrivals(stop, line)
        rows = [
            Arrival(
                normalize_line(a.line),
                (a.destination or "").strip(),
                a.bus,
                a.estimate_arrive if a.has_estimate else None,
                a.distance_bus,
            )
            for a in response.arrivals
            if line is None or normalize_line(a.line) == line
        ]
        return sorted(rows, key=lambda a: (a.eta_seconds is None, a.eta_seconds or 0, a.line))

    def stop_name(self, stop: str) -> str | None:
        with Session(self._engine) as session:
            row = session.get(Stop, stop)
            return row.name if row else None

    def risks(self, stops: list[str] | None, at: datetime) -> list[Risk]:
        loaded = self.models()
        if not loaded.bunching and not loaded.saturation:
            return []
        targets = stops or loaded.stops
        parameters = [m.parameters for m in (loaded.bunching, loaded.saturation) if m]
        lookback = max(_lookback(p) for p in parameters)
        observations, gaps = load_history(
            self._engine, at - timedelta(seconds=lookback), at, stops=targets
        )
        bunching: dict[Route, BunchingPrediction] = {}
        saturation: dict[Route, SaturationPrediction] = {}
        if loaded.bunching:
            series = build_series(observations, gaps, loaded.bunching.parameters)
            for pb in predict_bunching(series, loaded.bunching, at):
                bunching[Route(pb.line, pb.stop_id, pb.destination)] = pb
        if loaded.saturation:
            series = build_series(observations, gaps, loaded.saturation.parameters)
            for ps in predict_saturation(series, loaded.saturation, at):
                saturation[Route(ps.line, ps.stop_id, ps.destination)] = ps
        horizon = loaded.bunching.parameters.horizon_seconds // 60 if loaded.bunching else None
        risks = []
        for route in sorted(set(bunching) | set(saturation)):
            if route.stop_id not in targets:
                continue
            b = bunching.get(route)
            s = saturation.get(route)
            risks.append(
                Risk(
                    route,
                    b.probability if b else None,
                    b.status if b else None,
                    horizon if b else None,
                    s.probability if s else None,
                    s.status if s else None,
                    s.expected_wait_minutes if s else None,
                    s.minutes_since_last_bus if s else None,
                    s.threshold_minutes if s else None,
                )
            )
        return risks

    def status(self, at: datetime) -> Status:
        hour_ago = at - timedelta(hours=1)
        with Session(self._engine) as session:
            last = session.scalars(
                select(CollectionCycle)
                .where(CollectionCycle.status != "running")
                .order_by(CollectionCycle.started_at.desc())
                .limit(1)
            ).first()
            cycles, arrivals = session.execute(
                select(
                    func.count(CollectionCycle.id),
                    func.coalesce(func.sum(CollectionCycle.arrivals_inserted), 0),
                ).where(CollectionCycle.started_at >= hour_ago)
            ).one()
            gaps = session.scalar(
                select(func.count(CollectionGap.id)).where(CollectionGap.occurred_at >= hour_ago)
            )
        info = (
            CycleInfo(
                utc(last.started_at),
                last.status,
                last.stops_ok,
                last.stops_failed,
                last.arrivals_inserted,
            )
            if last
            else None
        )
        return Status(at, info, int(cycles), int(gaps or 0), int(arrivals), self.models())


def _lookback(p: Parameters) -> int:
    return p.lookback_seconds + p.bus_cooldown_seconds + p.near_seconds + p.max_latency_seconds
