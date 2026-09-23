from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from emt_collector.bunching.domain import Route
from emt_collector.saturation.domain import SaturationParameters

DemandMode = Literal["proxy", "uniform", "csv"]


class FrequencyParameters(SaturationParameters):
    """Umbrales de saturación más las restricciones operativas del reparto de frecuencias."""

    service_start_hour: int = Field(default=6, ge=0, le=23)
    service_end_hour: int = Field(default=23, ge=1, le=24)
    min_planned_headway_minutes: float = Field(default=3, gt=0)
    max_planned_headway_minutes: float = Field(default=20, gt=0)
    min_hour_samples: int = Field(default=5, ge=2)
    min_days: int = Field(default=3, ge=1)
    min_cycle_minutes: float = Field(default=20, gt=0)
    max_cycle_minutes: float = Field(default=240, gt=0)
    target_cv: float = Field(default=0.3, ge=0, lt=1)
    demand_mode: DemandMode = "proxy"


@dataclass(frozen=True)
class HourlyService:
    """Servicio observado en una ruta y hora local: intervalo medio, regularidad y espera."""

    route: Route
    hour: int
    headways: int
    days: int
    mean_headway_minutes: float
    cv: float
    saturated: int
    weight: float

    @property
    def expected_wait_minutes(self) -> float:
        """Espera media de un pasajero que llega al azar: E[H²] / 2E[H] = H̄ (1 + CV²) / 2."""
        return expected_wait(self.mean_headway_minutes, self.cv)

    @property
    def regular_wait_minutes(self) -> float:
        return self.mean_headway_minutes / 2

    @property
    def saturation_rate(self) -> float:
        return self.saturated / self.headways


def expected_wait(headway_minutes: float, cv: float) -> float:
    return headway_minutes * (1 + cv * cv) / 2


@dataclass(frozen=True)
class HourlyPlan:
    """Comparación actual frente a propuesto para una ruta y hora."""

    service: HourlyService
    current_buses: float
    proposed_buses: int
    proposed_headway_minutes: float

    @property
    def current_wait_minutes(self) -> float:
        return self.service.expected_wait_minutes

    @property
    def proposed_wait_minutes(self) -> float:
        return expected_wait(self.proposed_headway_minutes, self.service.cv)

    @property
    def weighted_saving_minutes(self) -> float:
        return self.service.weight * (self.current_wait_minutes - self.proposed_wait_minutes)


@dataclass(frozen=True)
class RoutePlan:
    route: Route
    cycle_minutes: float
    cycle_samples: int
    bus_hours: float
    hours: tuple[HourlyPlan, ...]

    @property
    def current_weighted_wait(self) -> float:
        return sum(h.service.weight * h.current_wait_minutes for h in self.hours)

    @property
    def proposed_weighted_wait(self) -> float:
        return sum(h.service.weight * h.proposed_wait_minutes for h in self.hours)

    @property
    def regular_weighted_wait(self) -> float:
        return sum(h.service.weight * h.service.regular_wait_minutes for h in self.hours)

    @property
    def total_weight(self) -> float:
        return sum(h.service.weight for h in self.hours)


class Skipped(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    line: str
    stop_id: str
    destination: str
    reason: str
