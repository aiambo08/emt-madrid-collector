from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field


def utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


class Parameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_seconds: int = Field(default=1200, ge=60)
    cluster_seconds: int = Field(default=180, ge=1)
    min_buses: int = Field(default=3, ge=2)
    near_seconds: int = Field(default=60, ge=0)
    near_metres: int = Field(default=150, gt=0)
    vanish_seconds: int = Field(default=180, ge=0)
    bus_cooldown_seconds: int = Field(default=600, gt=0)
    max_gap_seconds: int = Field(default=90, gt=0)
    max_latency_seconds: int = Field(default=120, ge=0)
    horizon_seconds: int = Field(default=900, ge=60)
    lookback_seconds: int = Field(default=3600, ge=1200)
    step_seconds: int = Field(default=300, ge=60)


@dataclass(frozen=True, order=True)
class Route:
    line: str
    stop_id: str
    destination: str

    @property
    def key(self) -> str:
        return f"{self.line} / {self.stop_id} / {self.destination}"


@dataclass(frozen=True)
class Observation:
    route: Route
    bus_id: int
    sample_ts: datetime
    ingested_at: datetime
    eta: int | None
    distance_m: int | None
    is_head: bool | None
    committed_at: datetime | None = None

    @property
    def available_at(self) -> datetime:
        return max(self.sample_ts, self.ingested_at, self.committed_at or self.ingested_at)


@dataclass(frozen=True)
class Gap:
    at: datetime
    stop_id: str | None = None
    line: str | None = None


@dataclass(frozen=True)
class Passage:
    route: Route
    bus_id: int
    at: datetime
    available_at: datetime


@dataclass(frozen=True)
class Event:
    route: Route
    start: datetime
    end: datetime
    previous_passage: datetime
    bus_ids: tuple[int, ...]
    gap_seconds: float
    span_seconds: float


@dataclass(frozen=True)
class Example:
    route: Route
    at: datetime
    label_end: datetime
    values: tuple[float, ...]
    target: int
