from __future__ import annotations

import heapq
import random
from bisect import bisect_left
from datetime import datetime, timedelta, timezone

from emt_collector.bunching.domain import Gap, Observation, Route
from emt_collector.bunching.features import MADRID
from emt_collector.saturation.demo import SERVICE_MINUTES, incident_probability, scheduled_headway

CYCLE_MINUTES = {"27": 96.0, "45": 72.0}


def synthetic_history(days: int = 14, seed: int = 7) -> tuple[list[Observation], list[Gap]]:
    """Como la demo de saturación, pero cada salida la realiza un bus concreto de una flota
    que vuelve a estar disponible tras el tiempo de ciclo: así el histórico permite inferir el
    ciclo y los buses en servicio. La línea 27 mantiene frecuencia de punta en horas valle
    (sobreoferta) y la 45 recorta demasiado a última hora (infraoferta), para que la
    redistribución tenga algo que corregir."""
    if not 3 <= days <= 90:
        raise ValueError("La demo requiere entre 3 y 90 días.")
    rng = random.Random(seed)
    observations = []
    gaps = []
    first = datetime(2026, 2, 2, 6, tzinfo=MADRID)
    for line, stop, destination in (("27", "DEMO-1", "NORTE"), ("45", "DEMO-2", "SUR")):
        route = Route(line, stop, destination)
        cycle = CYCLE_MINUTES[line]
        for day in range(days):
            local_start = first + timedelta(days=day)
            start = local_start.astimezone(timezone.utc)
            weekend = local_start.weekday() >= 5
            departures: list[float] = [rng.uniform(0, 4)]
            fleet: list[tuple[float, int]] = []
            next_bus = int(line) * 1000
            bus_ids: list[int] = []
            recovering = False
            while departures[-1] < SERVICE_MINUTES + 60:
                minute = departures[-1]
                local_hour = (6 + int(minute) // 60) % 24
                planned = _planned_headway(line, local_hour) * (1.25 if weekend else 1.0)
                headway = planned * rng.lognormvariate(0, 0.2)
                if recovering:
                    headway *= 0.55
                    recovering = False
                elif rng.random() < incident_probability(line, local_hour, weekend):
                    headway += rng.uniform(8, 16)
                    recovering = True
                if fleet and fleet[0][0] <= minute:
                    _, bus = heapq.heappop(fleet)
                else:
                    bus, next_bus = next_bus, next_bus + 1
                bus_ids.append(bus)
                heapq.heappush(fleet, (minute + cycle * rng.uniform(0.92, 1.08), bus))
                departures.append(minute + max(1.5, headway))
            bus_ids.append(next_bus)
            for minute in range(SERVICE_MINUTES):
                at = start + timedelta(minutes=minute)
                if day % 5 == 2 and 600 <= minute < 604:
                    gaps.append(Gap(at, stop, line))
                    continue
                upcoming = bisect_left(departures, float(minute))
                for index in range(upcoming, min(upcoming + 2, len(departures))):
                    eta = max(0, round((departures[index] - minute) * 60 + rng.gauss(0, 8)))
                    observations.append(
                        Observation(
                            route,
                            bus_ids[index],
                            at,
                            at,
                            eta,
                            max(0, round(eta * 2.2)),
                            False,
                        )
                    )
    return observations, gaps


def _planned_headway(line: str, local_hour: int) -> float:
    if line == "27" and 10 <= local_hour <= 13:
        return 5.0
    if line == "45" and local_hour >= 20:
        return 18.0
    return scheduled_headway(line, local_hour)
