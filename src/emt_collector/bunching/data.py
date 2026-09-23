from __future__ import annotations

from datetime import datetime

from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

from emt_collector.bunching.domain import Gap, Observation, Route, utc
from emt_collector.collector import normalize_line
from emt_collector.db.models import ArrivalEstimate, CollectionCycle, CollectionGap


def load_history(
    engine: Engine,
    start: datetime,
    end: datetime,
    max_rows: int = 1_000_000,
    stops: list[str] | None = None,
) -> tuple[list[Observation], list[Gap]]:
    if end <= start:
        raise ValueError("El final debe ser posterior al inicio.")
    query = (
        select(ArrivalEstimate, CollectionCycle.finished_at)
        .outerjoin(CollectionCycle, ArrivalEstimate.cycle_id == CollectionCycle.id)
        .where(
            ArrivalEstimate.sample_ts >= start,
            ArrivalEstimate.sample_ts < end,
            ArrivalEstimate.ingested_at < end,
            or_(ArrivalEstimate.cycle_id.is_(None), CollectionCycle.finished_at < end),
        )
        .order_by(ArrivalEstimate.sample_ts)
        .limit(max_rows + 1)
        .execution_options(yield_per=5000)
    )
    if stops:
        query = query.where(ArrivalEstimate.stop_id.in_(stops))
    observations: list[Observation] = []
    with Session(engine) as session:
        for row, finished_at in session.execute(query):
            if len(observations) >= max_rows:
                raise ValueError("Demasiadas muestras: reduce el rango de fechas (--start/--end).")
            observations.append(
                Observation(
                    Route(normalize_line(row.line), row.stop_id, (row.destination or "").strip()),
                    row.bus_id,
                    utc(row.sample_ts),
                    utc(row.ingested_at),
                    row.estimate_seconds,
                    row.distance_m,
                    row.is_head,
                    utc(finished_at) if finished_at else None,
                )
            )
        gap_rows = session.scalars(
            select(CollectionGap).where(
                CollectionGap.occurred_at >= start,
                CollectionGap.occurred_at < end,
            )
        )
        gaps = [
            Gap(utc(row.occurred_at), row.stop_id, normalize_line(row.line) if row.line else None)
            for row in gap_rows
        ]
    return observations, gaps
