from __future__ import annotations

import random
from bisect import bisect_left
from datetime import datetime, timedelta, timezone

from emt_collector.bunching.domain import Gap, Observation, Route
from emt_collector.bunching.features import MADRID

SERVICE_MINUTES = 17 * 60  # 06:00 → 23:00 hora local


def scheduled_headway(line: str, local_hour: int) -> float:
    rush = {7, 8, 9, 17, 18, 19} if line == "27" else {8, 9, 18, 19, 20}
    if local_hour in rush:
        return 5.0
    return 12.0 if local_hour >= 21 or local_hour < 7 else 8.0


def incident_probability(line: str, local_hour: int, weekend: bool) -> float:
    if weekend:
        return 0.03
    peak = {8, 9, 18, 19} if line == "27" else {9, 18, 19}
    return 0.14 if local_hour in peak else 0.04


def synthetic_history(days: int = 28, seed: int = 42) -> tuple[list[Observation], list[Gap]]:
    """Servicio con frecuencia horaria variable, incidencias que alargan el intervalo y su
    recuperación (el bus siguiente llega antes), ruido de ETA y cortes de telemetría."""
    if not 10 <= days <= 90:
        raise ValueError("La demo requiere entre 10 y 90 días.")
    rng = random.Random(seed)
    observations = []
    gaps = []
    first = datetime(2026, 1, 12, 6, tzinfo=MADRID)
    for line, stop, destination in (("27", "DEMO-1", "NORTE"), ("45", "DEMO-2", "SUR")):
        route = Route(line, stop, destination)
        for day in range(days):
            local_start = first + timedelta(days=day)
            start = local_start.astimezone(timezone.utc)
            weekend = local_start.weekday() >= 5
            departures: list[float] = [rng.uniform(0, 4)]
            recovering = False
            while departures[-1] < SERVICE_MINUTES + 60:
                minute = departures[-1]
                local_hour = (6 + int(minute) // 60) % 24
                planned = scheduled_headway(line, local_hour) * (1.25 if weekend else 1.0)
                headway = planned * rng.lognormvariate(0, 0.22)
                if recovering:
                    headway *= 0.55
                    recovering = False
                elif rng.random() < incident_probability(line, local_hour, weekend):
                    headway += rng.uniform(8, 16)
                    recovering = True
                departures.append(minute + max(1.5, headway))
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
    return observations, gaps
