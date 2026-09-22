from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import ColumnClause, Engine, Integer, create_engine, literal_column, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Executable

from emt_collector.db.models import (
    ArrivalEstimate,
    Base,
    BusPosition,
    CollectionCycle,
    CollectionGap,
    Stop,
)

log = structlog.get_logger(__name__)

HYPERTABLES = {
    "bus_positions": "sample_ts",
    "arrival_estimates": "sample_ts",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, future=True)


def init_schema(engine: Engine, use_timescale: bool = True) -> bool:
    """Create tables and (optionally) TimescaleDB hypertables. Returns whether Timescale is on."""
    Base.metadata.create_all(engine)
    if not use_timescale or engine.dialect.name != "postgresql":
        return False
    with engine.begin() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        except DBAPIError as exc:
            log.warning("db.timescale_unavailable", error=str(exc.orig))
            return False
    for table, column in HYPERTABLES.items():
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"SELECT create_hypertable('{table}', '{column}', "
                    "if_not_exists => TRUE, migrate_data => TRUE, "
                    "chunk_time_interval => INTERVAL '1 day')"
                )
            )
    log.info("db.timescale_enabled", hypertables=list(HYPERTABLES))
    return True


class Repository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._dialect = engine.dialect.name

    def _insert_ignore(self, table: Any, rows: Sequence[dict[str, Any]]) -> int:
        """Bulk insert skipping rows whose natural key already exists. Returns rows inserted."""
        if not rows:
            return 0
        marker: ColumnClause[int] = literal_column("1", type_=Integer)
        stmt: Executable
        if self._dialect == "postgresql":
            stmt = postgresql.insert(table).values(rows).on_conflict_do_nothing().returning(marker)
        elif self._dialect == "sqlite":
            stmt = sqlite.insert(table).values(rows).on_conflict_do_nothing().returning(marker)
        else:
            raise RuntimeError(f"unsupported dialect {self._dialect}")
        with self._engine.begin() as conn:
            return len(conn.execute(stmt).fetchall())

    # -- cycles / gaps ---------------------------------------------------------------------

    def start_cycle(self, started_at: datetime, stops_requested: int) -> int:
        with Session(self._engine) as session, session.begin():
            cycle = CollectionCycle(
                started_at=started_at, status="running", stops_requested=stops_requested
            )
            session.add(cycle)
            session.flush()
            return cycle.id

    def finish_cycle(self, cycle_id: int, **fields: Any) -> None:
        with Session(self._engine) as session, session.begin():
            cycle = session.get(CollectionCycle, cycle_id)
            if cycle is None:
                return
            for key, value in fields.items():
                setattr(cycle, key, value)

    def record_gap(
        self,
        *,
        scope: str,
        kind: str,
        detail: str | None = None,
        cycle_id: int | None = None,
        stop_id: str | None = None,
        line: str | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            session.add(
                CollectionGap(
                    cycle_id=cycle_id,
                    occurred_at=occurred_at or utcnow(),
                    scope=scope,
                    stop_id=stop_id,
                    line=line,
                    kind=kind,
                    detail=(detail or "")[:2000] or None,
                )
            )

    def record_gaps(self, gaps: Iterable[dict[str, Any]]) -> int:
        rows = list(gaps)
        if not rows:
            return 0
        with Session(self._engine) as session, session.begin():
            session.add_all(CollectionGap(**g) for g in rows)
        return len(rows)

    # -- samples ---------------------------------------------------------------------------

    def insert_positions(self, rows: Sequence[dict[str, Any]]) -> int:
        return self._insert_ignore(BusPosition.__table__, rows)

    def insert_arrivals(self, rows: Sequence[dict[str, Any]]) -> int:
        return self._insert_ignore(ArrivalEstimate.__table__, rows)

    # -- stops cache -----------------------------------------------------------------------

    def upsert_stops(self, stops: Sequence[dict[str, Any]]) -> None:
        if not stops:
            return
        with Session(self._engine) as session, session.begin():
            for row in stops:
                session.merge(Stop(**row))

    def cached_stops(self) -> list[Stop]:
        with Session(self._engine) as session:
            return list(session.scalars(select(Stop)).all())

    def stops_cache_age(self) -> float | None:
        """Seconds since the oldest cached stop was refreshed, or None when the cache is empty."""
        with Session(self._engine) as session:
            oldest = session.scalar(
                select(Stop.updated_at).order_by(Stop.updated_at.asc()).limit(1)
            )
        if oldest is None:
            return None
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        return (utcnow() - oldest).total_seconds()
