from __future__ import annotations

import random
from bisect import bisect_left
from datetime import datetime, timedelta, timezone

from emt_collector.bunching.domain import Gap, Observation, Route
from emt_collector.bunching.features import MADRID
from emt_collector.saturation.demo import SERVICE_MINUTES, incident_probability, scheduled_headway

TREATED_LINE = "27"
CONTROL_LINE = "45"
HEADWAY_FACTOR = 0.8
INCIDENT_FACTOR = 0.35


def synthetic_history(
    days: int = 14, seed: int = 11
) -> tuple[list[Observation], list[Gap], datetime]:
    """Dos rutas con el mismo generador que la demo de saturación, donde parte de las
    incidencias largas terminan con tres buses llegando en pelotón (bunching). A mitad del
    periodo la línea 27 recibe una medida (refuerzo de frecuencia y regulación: intervalos un
    20 % más cortos y muchas menos incidencias); la línea 45 no cambia y sirve de control.
    Devuelve también el instante del evento."""
    if not 4 <= days <= 90:
        raise ValueError("La demo requiere entre 4 y 90 días.")
    rng = random.Random(seed)
    observations = []
    gaps = []
    first = datetime(2026, 3, 2, 6, tzinfo=MADRID)
    event = (first + timedelta(days=days // 2)).replace(hour=0).astimezone(timezone.utc)
    for line, stop, destination in (
        (TREATED_LINE, "DEMO-1", "NORTE"),
        (CONTROL_LINE, "DEMO-2", "SUR"),
    ):
        route = Route(line, stop, destination)
        for day in range(days):
            local_start = first + timedelta(days=day)
            start = local_start.astimezone(timezone.utc)
            treated = line == TREATED_LINE and start >= event
            weekend = local_start.weekday() >= 5
            departures: list[float] = [rng.uniform(0, 4)]
            bunched = 0
            recovering = False
            while departures[-1] < SERVICE_MINUTES + 60:
                minute = departures[-1]
                local_hour = (6 + int(minute) // 60) % 24
                planned = scheduled_headway(line, local_hour) * (1.25 if weekend else 1.0)
                incident = incident_probability(line, local_hour, weekend)
                if treated:
                    planned *= HEADWAY_FACTOR
                    incident *= INCIDENT_FACTOR
                headway = planned * rng.lognormvariate(0, 0.22)
                if bunched:
                    headway = rng.uniform(0.8, 1.2)
                    bunched -= 1
                elif recovering:
                    headway *= 0.55
                    recovering = False
                elif rng.random() < incident:
                    headway += rng.uniform(12, 20)
                    if rng.random() < 0.5:
                        bunched = 2
                    else:
                        recovering = True
                departures.append(minute + max(0.8, headway))
            for minute in range(SERVICE_MINUTES):
                at = start + timedelta(minutes=minute)
                if day % 4 == 1 and 480 <= minute < 484:
                    gaps.append(Gap(at, stop, line))
                    continue
                upcoming = bisect_left(departures, float(minute))
                for index in range(upcoming, min(upcoming + 2, len(departures))):
                    eta = max(0, round((departures[index] - minute) * 60 + rng.gauss(0, 8)))
                    observations.append(
                        Observation(
                            route,
                            int(line) * 1000 + index % 40,
                            at,
                            at,
                            eta,
                            max(0, round(eta * 2.2)),
                            False,
                        )
                    )
    return observations, gaps, event
