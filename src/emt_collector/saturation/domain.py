from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import Field

from emt_collector.bunching.domain import Parameters, Route


class SaturationParameters(Parameters):
    """Parámetros del detector de bunching más los umbrales de saturación."""

    ratio: float = Field(default=1.5, gt=1)
    min_headway_seconds: int = Field(default=720, ge=60)
    max_wait_seconds: int = Field(default=5400, ge=600)


@dataclass(frozen=True)
class Headway:
    """Intervalo entre dos pasos inferidos consecutivos de una ruta."""

    route: Route
    previous_bus: int
    bus: int
    start: datetime
    end: datetime
    available_at: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass(frozen=True)
class Window:
    """Instante etiquetable: features conocidas en `at` y la siguiente llegada real."""

    route: Route
    at: datetime
    label_end: datetime
    values: tuple[float, ...]
    headway: Headway

    @property
    def wait_minutes(self) -> float:
        return (self.headway.end - self.at).total_seconds() / 60


@dataclass(frozen=True)
class SaturatedHeadway:
    headway: Headway
    reference_minutes: float
    threshold_minutes: float
