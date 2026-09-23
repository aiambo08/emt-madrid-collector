from __future__ import annotations

import math

from emt_collector.bunching.detector import Series
from emt_collector.frequency.domain import (
    FrequencyParameters,
    HourlyPlan,
    HourlyService,
    RoutePlan,
    Skipped,
    expected_wait,
)
from emt_collector.frequency.service import cycle_minutes


def allocate(
    service: list[HourlyService], cycle: float, parameters: FrequencyParameters
) -> list[HourlyPlan]:
    """Reparte las mismas horas-bus observadas entre franjas minimizando la espera ponderada.

    Con `n` buses en servicio el intervalo es `cycle / n` y la espera media
    `cycle / n · (1 + CV²) / 2`; la asignación greedy por ganancia marginal es óptima para
    este objetivo convexo con buses enteros. La regularidad (CV) de cada hora se mantiene: la
    propuesta solo mueve buses, no corrige el bunching."""
    current = [cycle / row.mean_headway_minutes for row in service]
    budget = sum(current)
    floor = [max(1, math.ceil(cycle / parameters.max_planned_headway_minutes)) for _ in service]
    ceiling = [max(1, math.floor(cycle / parameters.min_planned_headway_minutes)) for _ in service]
    if sum(floor) > budget:
        floor = [1] * len(service)
    buses = list(floor)
    remaining = budget - sum(buses)
    while remaining >= 1:
        best, gain = -1, 0.0
        for index, row in enumerate(service):
            if buses[index] >= ceiling[index]:
                continue
            saving = row.weight * (
                expected_wait(cycle / buses[index], row.cv)
                - expected_wait(cycle / (buses[index] + 1), row.cv)
            )
            if saving > gain:
                best, gain = index, saving
        if best < 0:
            break
        buses[best] += 1
        remaining -= 1
    return [
        HourlyPlan(row, current[index], buses[index], cycle / buses[index])
        for index, row in enumerate(service)
    ]


def plan_route(
    series: Series, service: list[HourlyService], parameters: FrequencyParameters
) -> RoutePlan | Skipped:
    route = series.route
    if not service:
        return Skipped(
            line=route.line,
            stop_id=route.stop_id,
            destination=route.destination,
            reason=(
                f"menos de {parameters.min_days} días con datos o ninguna hora con "
                f"≥ {parameters.min_hour_samples} intervalos completos"
            ),
        )
    cycle, samples = cycle_minutes(series, parameters)
    if cycle is None:
        return Skipped(
            line=route.line,
            stop_id=route.stop_id,
            destination=route.destination,
            reason="ningún bus vuelve a pasar por la parada dentro del rango de ciclo admitido",
        )
    if sum(row.weight for row in service) <= 0:
        return Skipped(
            line=route.line,
            stop_id=route.stop_id,
            destination=route.destination,
            reason="todos los pesos de demanda son cero",
        )
    hours = allocate(service, cycle, parameters)
    return RoutePlan(route, cycle, samples, sum(h.current_buses for h in hours), tuple(hours))
