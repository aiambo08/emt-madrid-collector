from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CollectionCycle(Base):
    """One scheduler tick. Every stored sample points back to the cycle that produced it."""

    __tablename__ = "collection_cycles"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # running|ok|partial|failed|empty
    stops_requested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stops_ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stops_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    api_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    api_retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reauths: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    positions_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    arrivals_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class CollectionGap(Base):
    """A hole in the dataset: a failed/empty cycle or a failed stop within a cycle."""

    __tablename__ = "collection_gaps"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    cycle_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("collection_cycles.id")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # cycle|stop|scheduler
    stop_id: Mapped[str | None] = mapped_column(String(16))
    line: Mapped[str | None] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # network|auth|api_error|empty|...
    detail: Mapped[str | None] = mapped_column(Text)


class Stop(Base):
    """Cached stop metadata (refreshed periodically from the line-stops endpoint)."""

    __tablename__ = "stops"

    stop_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    lines: Mapped[str | None] = mapped_column(Text)  # comma-separated, informational
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BusPosition(Base):
    __tablename__ = "bus_positions"
    __table_args__ = (
        PrimaryKeyConstraint("line", "bus_id", "sample_ts", name="pk_bus_positions"),
        Index("ix_bus_positions_sample_ts", "sample_ts"),
        Index("ix_bus_positions_bus_ts", "bus_id", "sample_ts"),
    )

    line: Mapped[str] = mapped_column(String(16), nullable=False)
    bus_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    destination: Mapped[str | None] = mapped_column(String(128))
    position_type: Mapped[str | None] = mapped_column(String(8))
    observed_from_stop: Mapped[str | None] = mapped_column(String(16))
    cycle_id: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"))


class ArrivalEstimate(Base):
    __tablename__ = "arrival_estimates"
    __table_args__ = (
        PrimaryKeyConstraint("stop_id", "line", "bus_id", "sample_ts", name="pk_arrival_estimates"),
        Index("ix_arrival_estimates_sample_ts", "sample_ts"),
        Index("ix_arrival_estimates_line_stop_ts", "line", "stop_id", "sample_ts"),
    )

    stop_id: Mapped[str] = mapped_column(String(16), nullable=False)
    line: Mapped[str] = mapped_column(String(16), nullable=False)
    bus_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    estimate_seconds: Mapped[int | None] = mapped_column(Integer)
    distance_m: Mapped[int | None] = mapped_column(Integer)
    destination: Mapped[str | None] = mapped_column(String(128))
    is_head: Mapped[bool | None] = mapped_column(Boolean)
    deviation: Mapped[int | None] = mapped_column(Integer)
    cycle_id: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"))
