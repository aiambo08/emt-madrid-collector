from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from emt_collector.bunching.domain import Route
from emt_collector.saturation.domain import SaturationParameters

Period = Literal["before", "after"]
METRICS: tuple[str, ...] = (
    "mean_headway_minutes",
    "expected_wait_minutes",
    "saturation_rate",
    "episodes_per_day",
)
METRIC_LABELS = {
    "mean_headway_minutes": "Intervalo medio (min)",
    "expected_wait_minutes": "Espera media (min)",
    "saturation_rate": "Intervalos saturados",
    "episodes_per_day": "Episodios de bunching / día",
}


class ImpactParameters(SaturationParameters):
    """Umbrales del detector y de saturación más los requisitos de las ventanas comparadas."""

    min_headways: int = Field(default=20, ge=2)
    min_days: int = Field(default=2, ge=1)
    bootstrap_samples: int = Field(default=1000, ge=100)
    confidence: float = Field(default=0.95, gt=0.5, lt=1)
    seed: int = 0


@dataclass(frozen=True)
class WindowMetrics:
    """Servicio observado en una ruta durante una de las dos ventanas."""

    route: Route
    period: Period
    start: datetime
    end: datetime
    headway_minutes: tuple[float, ...]
    saturated: tuple[bool, ...]
    episodes_by_day: tuple[int, ...]
    hourly_mean: dict[int, float]

    @property
    def headways(self) -> int:
        return len(self.headway_minutes)

    @property
    def days(self) -> int:
        return len(self.episodes_by_day)

    @property
    def episodes(self) -> int:
        return sum(self.episodes_by_day)

    @property
    def mean_headway_minutes(self) -> float:
        return sum(self.headway_minutes) / len(self.headway_minutes)

    @property
    def cv(self) -> float:
        mean = self.mean_headway_minutes
        variance = sum((h - mean) ** 2 for h in self.headway_minutes) / len(self.headway_minutes)
        return math.sqrt(variance) / mean

    @property
    def expected_wait_minutes(self) -> float:
        """E[H²] / 2E[H]: espera media de quien llega al azar a la parada."""
        total = sum(self.headway_minutes)
        return sum(h * h for h in self.headway_minutes) / (2 * total)

    @property
    def saturation_rate(self) -> float:
        return sum(self.saturated) / len(self.saturated)

    @property
    def episodes_per_day(self) -> float:
        return self.episodes / self.days

    def metric(self, name: str) -> float:
        value: float = {
            "mean_headway_minutes": self.mean_headway_minutes,
            "expected_wait_minutes": self.expected_wait_minutes,
            "saturation_rate": self.saturation_rate,
            "episodes_per_day": self.episodes_per_day,
        }[name]
        return value


@dataclass(frozen=True)
class Change:
    """Diferencia después − antes de una métrica con intervalo bootstrap; `p_value` sólo para
    la distribución de intervalos (Mann-Whitney)."""

    metric: str
    before: float
    after: float
    ci_low: float
    ci_high: float
    p_value: float | None = None

    @property
    def delta(self) -> float:
        return self.after - self.before

    @property
    def relative(self) -> float | None:
        return None if self.before == 0 else self.delta / self.before

    @property
    def significant(self) -> bool:
        return not (self.ci_low <= 0 <= self.ci_high)


@dataclass(frozen=True)
class RouteImpact:
    route: Route
    before: WindowMetrics
    after: WindowMetrics
    changes: tuple[Change, ...]
    role: Literal["treated", "control"]

    def change(self, metric: str) -> Change:
        return next(c for c in self.changes if c.metric == metric)


@dataclass(frozen=True)
class DifferenceInDifferences:
    """(después − antes) en rutas tratadas menos lo mismo en rutas de control."""

    metric: str
    treated_delta: float
    control_delta: float
    ci_low: float
    ci_high: float

    @property
    def estimate(self) -> float:
        return self.treated_delta - self.control_delta

    @property
    def significant(self) -> bool:
        return not (self.ci_low <= 0 <= self.ci_high)


class Skipped(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    line: str
    stop_id: str
    destination: str
    reason: str
