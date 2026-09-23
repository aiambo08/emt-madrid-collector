from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from pydantic import BaseModel, ConfigDict, Field

from emt_collector.bunching.detector import Series
from emt_collector.bunching.domain import Route
from emt_collector.bunching.features import MADRID, features
from emt_collector.saturation.domain import Headway, SaturatedHeadway, SaturationParameters, Window

MIN_REFERENCE_SAMPLES = 5


def headways(series: Series) -> list[Headway]:
    """Intervalos consecutivos con cobertura completa entre ambos pasos."""
    p = series.parameters
    confirmation = timedelta(seconds=p.near_seconds + p.max_latency_seconds)
    passages = [item for item in series.passages if item.available_at <= series.times[-1]]
    result = []
    for previous, current in zip(passages, passages[1:], strict=False):
        if previous.bus_id == current.bus_id or current.at <= previous.at:
            continue
        known_at = max(previous.available_at, current.available_at)
        if series.covered(previous.at, known_at) and series.covered(
            current.at, min(current.at + confirmation, series.times[-1])
        ):
            result.append(
                Headway(
                    series.route,
                    previous.bus_id,
                    current.bus_id,
                    previous.at,
                    current.at,
                    known_at,
                )
            )
    return result


def windows(series: Series, intervals: list[Headway], end: datetime) -> list[Window]:
    """Un instante cada `step_seconds` cuya siguiente llegada real es conocida."""
    p = series.parameters
    if not isinstance(p, SaturationParameters):
        raise TypeError("La serie debe construirse con SaturationParameters.")
    if not series.times or not intervals:
        return []
    first = series.times[0] + timedelta(seconds=p.lookback_seconds)
    at = datetime.fromtimestamp(
        math.ceil(first.timestamp() / p.step_seconds) * p.step_seconds, tz=first.tzinfo
    )
    confirmation = timedelta(seconds=p.near_seconds + p.max_latency_seconds)
    last = min(end, series.times[-1])
    starts = [item.start for item in intervals]
    result = []
    while at <= last:
        index = bisect_right(starts, at) - 1
        if index >= 0:
            interval = intervals[index]
            label_end = interval.end + confirmation
            if (
                at < interval.end
                and label_end <= last
                and (interval.end - at).total_seconds() <= p.max_wait_seconds
                and series.covered(at, label_end)
            ):
                values = features(series, at)
                if values is not None:
                    result.append(Window(series.route, at, label_end, values, interval))
        at += timedelta(seconds=p.step_seconds)
    return result


class RouteReference(BaseModel):
    """Mediana del intervalo entre buses por hora local de la llegada."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    line: str
    stop_id: str
    destination: str
    hourly_minutes: list[float] = Field(min_length=24, max_length=24)
    samples: int = Field(ge=1)

    @property
    def route(self) -> Route:
        return Route(self.line, self.stop_id, self.destination)


def hour_of(value: datetime) -> int:
    return value.astimezone(MADRID).hour


def build_reference(intervals: list[Headway]) -> list[RouteReference]:
    """Mediana por ruta y hora, con respaldo en la mediana de la ruta si hay pocas muestras."""
    by_route: dict[Route, list[Headway]] = defaultdict(list)
    for item in intervals:
        by_route[item.route].append(item)
    result = []
    for route, rows in sorted(by_route.items()):
        overall = median(item.minutes for item in rows)
        by_hour: dict[int, list[float]] = defaultdict(list)
        for item in rows:
            by_hour[hour_of(item.end)].append(item.minutes)
        result.append(
            RouteReference(
                line=route.line,
                stop_id=route.stop_id,
                destination=route.destination,
                hourly_minutes=[
                    median(values)
                    if (values := by_hour.get(hour)) and len(values) >= MIN_REFERENCE_SAMPLES
                    else overall
                    for hour in range(24)
                ],
                samples=len(rows),
            )
        )
    return result


class Threshold:
    """Resuelve el umbral de saturación de una ruta a una hora dada."""

    def __init__(self, reference: list[RouteReference], parameters: SaturationParameters) -> None:
        self._reference = {item.route: item for item in reference}
        self._parameters = parameters

    def routes(self) -> set[Route]:
        return set(self._reference)

    def reference_minutes(self, route: Route, at: datetime) -> float | None:
        item = self._reference.get(route)
        return item.hourly_minutes[hour_of(at)] if item else None

    def minutes(self, route: Route, at: datetime) -> float | None:
        reference = self.reference_minutes(route, at)
        if reference is None:
            return None
        return max(reference * self._parameters.ratio, self._parameters.min_headway_seconds / 60)

    def is_saturated(self, item: Headway) -> bool | None:
        threshold = self.minutes(item.route, item.end)
        return None if threshold is None else item.minutes >= threshold

    def saturated(self, intervals: list[Headway]) -> list[SaturatedHeadway]:
        result = []
        for item in intervals:
            reference = self.reference_minutes(item.route, item.end)
            threshold = self.minutes(item.route, item.end)
            if reference is not None and threshold is not None and item.minutes >= threshold:
                result.append(SaturatedHeadway(item, reference, threshold))
        return result
