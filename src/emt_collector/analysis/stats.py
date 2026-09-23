from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from emt_collector.bunching.data import load_history
from emt_collector.bunching.detector import Series, build_series
from emt_collector.bunching.domain import Parameters, utc
from emt_collector.db.models import ArrivalEstimate, BusPosition, CollectionCycle, CollectionGap

MIN_DAYS = 7
MIN_PASSAGES_PER_DAY = 40
MIN_CYCLE_RETURNS = 10
MIN_CYCLE_MINUTES = 20
MAX_CYCLE_MINUTES = 240


@dataclass(frozen=True)
class TableStats:
    arrivals: int
    first_sample: str | None
    last_sample: str | None
    days: float
    stops: int
    lines: int
    buses: int
    positions: int
    position_buses: int


@dataclass(frozen=True)
class CycleStats:
    total: int
    by_status: dict[str, int]
    first: str | None
    last: str | None
    span_days: float
    expected_per_day: float
    observed_per_day: float
    stops_failed: int
    api_requests: int
    api_retries: int
    requests_per_day: int
    gaps: int
    gaps_by_kind: dict[str, int]


@dataclass(frozen=True)
class RouteStats:
    line: str
    stop_id: str
    destination: str
    observations: int
    buses: int
    days_with_data: int
    passages: int
    vanish_passages: int
    passages_per_day: float
    median_headway_minutes: float | None
    cycle_returns: int
    median_cycle_minutes: float | None


def _iso(value: datetime | None) -> str | None:
    return utc(value).isoformat() if value else None


def _tables(session: Session, start: datetime, end: datetime) -> TableStats:
    arrivals = session.execute(
        select(
            func.count(),
            func.min(ArrivalEstimate.sample_ts),
            func.max(ArrivalEstimate.sample_ts),
            func.count(func.distinct(ArrivalEstimate.stop_id)),
            func.count(func.distinct(ArrivalEstimate.line)),
            func.count(func.distinct(ArrivalEstimate.bus_id)),
        ).where(ArrivalEstimate.sample_ts >= start, ArrivalEstimate.sample_ts < end)
    ).one()
    positions = session.execute(
        select(func.count(), func.count(func.distinct(BusPosition.bus_id))).where(
            BusPosition.sample_ts >= start, BusPosition.sample_ts < end
        )
    ).one()
    first, last = arrivals[1], arrivals[2]
    days = (utc(last) - utc(first)).total_seconds() / 86400 if first and last else 0.0
    return TableStats(
        arrivals[0],
        _iso(first),
        _iso(last),
        round(days, 2),
        arrivals[3],
        arrivals[4],
        arrivals[5],
        positions[0],
        positions[1],
    )


def _cycles(session: Session, start: datetime, end: datetime, interval_seconds: int) -> CycleStats:
    rows = session.execute(
        select(
            CollectionCycle.status,
            func.count(),
            func.sum(CollectionCycle.stops_failed),
            func.sum(CollectionCycle.api_requests),
            func.sum(CollectionCycle.api_retries),
            func.min(CollectionCycle.started_at),
            func.max(CollectionCycle.started_at),
        )
        .where(CollectionCycle.started_at >= start, CollectionCycle.started_at < end)
        .group_by(CollectionCycle.status)
    ).all()
    by_status = {status: count for status, count, *_ in rows}
    total = sum(by_status.values())
    first = min((utc(r[5]) for r in rows), default=None)
    last = max((utc(r[6]) for r in rows), default=None)
    span_days = 0.0
    if first and last:
        span_days = max((last - first).total_seconds(), interval_seconds) / 86400
    requests = int(sum(r[3] or 0 for r in rows))
    gap_rows = session.execute(
        select(CollectionGap.kind, func.count())
        .where(CollectionGap.occurred_at >= start, CollectionGap.occurred_at < end)
        .group_by(CollectionGap.kind)
    ).all()
    return CycleStats(
        total,
        by_status,
        _iso(first),
        _iso(last),
        round(span_days, 2),
        round(86400 / interval_seconds, 1),
        round(total / span_days, 1) if span_days else 0.0,
        int(sum(r[2] or 0 for r in rows)),
        requests,
        int(sum(r[4] or 0 for r in rows)),
        round(requests / span_days) if span_days else 0,
        sum(count for _, count in gap_rows),
        {kind: count for kind, count in gap_rows},
    )


def _route(series: Series, near_passages: int) -> RouteStats:
    passage_days = max(len({p.at.date() for p in series.passages}), 1)
    headways = [
        (b.at - a.at).total_seconds() / 60
        for a, b in zip(series.passages, series.passages[1:], strict=False)
        if a.bus_id != b.bus_id
    ]
    by_bus: dict[int, list[datetime]] = defaultdict(list)
    for passage in series.passages:
        by_bus[passage.bus_id].append(passage.at)
    cycles = [
        minutes
        for times in by_bus.values()
        for a, b in zip(times, times[1:], strict=False)
        if MIN_CYCLE_MINUTES <= (minutes := (b - a).total_seconds() / 60) <= MAX_CYCLE_MINUTES
    ]
    return RouteStats(
        series.route.line,
        series.route.stop_id,
        series.route.destination,
        len(series.observations),
        len({row.bus_id for row in series.observations}),
        len({row.sample_ts.date() for row in series.observations}),
        len(series.passages),
        len(series.passages) - near_passages,
        round(len(series.passages) / passage_days, 1),
        round(median(headways), 1) if headways else None,
        len(cycles),
        round(median(cycles), 1) if cycles else None,
    )


def _hints(
    tables: TableStats,
    cycles: CycleStats,
    routes: list[RouteStats],
    empty_destination: int,
    quota: int,
) -> list[str]:
    if not tables.arrivals:
        return ["Sin llegadas en el periodo: comprueba que el recolector está en marcha."]
    hints = []
    if tables.days < MIN_DAYS:
        hints.append(
            f"Solo {tables.days} días de histórico; los modelos necesitan al menos {MIN_DAYS}."
        )
    if cycles.observed_per_day < cycles.expected_per_day * 0.9:
        hints.append(
            f"{cycles.observed_per_day} ciclos/día frente a {cycles.expected_per_day} esperados: "
            "el recolector ha estado parado o salta ciclos por solapamiento."
        )
    bad = cycles.by_status.get("failed", 0) + cycles.by_status.get("partial", 0)
    if cycles.total and bad / cycles.total > 0.05:
        hints.append(
            f"{bad} de {cycles.total} ciclos fallidos o parciales: revisa collection_gaps."
        )
    if cycles.requests_per_day > quota:
        hints.append(f"{cycles.requests_per_day} peticiones/día superan el presupuesto de {quota}.")
    if empty_destination:
        hints.append(f"{empty_destination} llegadas sin destino se descartan (sin sentido).")
    weak = sum(r.passages_per_day < MIN_PASSAGES_PER_DAY for r in routes)
    if weak:
        hints.append(
            f"{weak} rutas con menos de {MIN_PASSAGES_PER_DAY} pasos/día: pocas muestras para "
            "headways por hora."
        )
    no_cycle = sum(r.cycle_returns < MIN_CYCLE_RETURNS for r in routes)
    if no_cycle:
        hints.append(
            f"{no_cycle} rutas con menos de {MIN_CYCLE_RETURNS} retornos del mismo bus: "
            "emt-frequency no podrá estimar su ciclo."
        )
    return hints


def history_stats(
    engine: Engine,
    start: datetime,
    end: datetime,
    interval_seconds: int,
    quota: int,
    parameters: Parameters | None = None,
) -> dict[str, object]:
    """Coverage, collector health and inferred-passage quality of the stored history."""
    parameters = parameters or Parameters()
    with Session(engine) as session:
        tables = _tables(session, start, end)
        cycles = _cycles(session, start, end, interval_seconds)
    observations, gaps = load_history(engine, start, end)
    empty_destination = sum(1 for row in observations if not row.route.destination)
    strict = parameters.model_copy(update={"vanish_seconds": 0})
    near = {s.route: len(s.passages) for s in build_series(observations, gaps, strict)}
    routes = [
        _route(series, near.get(series.route, 0))
        for series in build_series(observations, gaps, parameters)
    ]
    return {
        "window": {"start": _iso(start), "end": _iso(end)},
        "history": asdict(tables),
        "cycles": asdict(cycles),
        "routes": [asdict(route) for route in routes],
        "hints": _hints(tables, cycles, routes, empty_destination, quota),
    }


def default_window(days: int, now: datetime) -> tuple[datetime, datetime]:
    return now - timedelta(days=days), now
