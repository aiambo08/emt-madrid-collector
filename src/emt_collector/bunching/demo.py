from __future__ import annotations

import random
from bisect import bisect_left
from datetime import datetime, timedelta, timezone

from emt_collector.bunching.domain import Gap, Observation, Route
from emt_collector.bunching.features import MADRID


def synthetic_history(days: int = 28, seed: int = 42) -> tuple[list[Observation], list[Gap]]:
    """Minute samples of two approaching buses, including service and telemetry gaps."""
    if not 10 <= days <= 90:
        raise ValueError("La demo requiere entre 10 y 90 días.")
    rng = random.Random(seed)
    observations = []
    gaps = []
    first = datetime(2026, 1, 12, 6, tzinfo=MADRID)
    for line, stop, destination in (("27", "DEMO-1", "NORTE"), ("45", "DEMO-2", "SUR")):
        route = Route(line, stop, destination)
        for day in range(days):
            start = (first + timedelta(days=day)).astimezone(timezone.utc)
            service_minutes = 17 * 60
            departures: list[int] = [0]
            while departures[-1] < service_minutes + 40:
                minute = departures[-1]
                local_hour = 6 + minute // 60
                rush = local_hour in ({8, 9, 18, 19} if line == "27" else {7, 8, 17, 18})
                if rng.random() < (0.38 if rush else 0.012):
                    first_bus = minute + rng.randint(22, 30)
                    departures.extend([first_bus, first_bus + 1, first_bus + 2])
                else:
                    departures.append(minute + rng.randint(6, 10))
            for minute in range(service_minutes):
                at = start + timedelta(minutes=minute)
                if day % 3 == 0 and 390 <= minute < 394:
                    gaps.append(Gap(at, stop, line))
                    continue
                upcoming = bisect_left(departures, minute)
                for index in range(upcoming, min(upcoming + 2, len(departures))):
                    eta = max(0, (departures[index] - minute) * 60 + rng.randint(-10, 10))
                    observations.append(
                        Observation(
                            route,
                            int(line) * 1000 + index % 30,
                            at,
                            at,
                            eta,
                            max(0, eta * 2),
                            False,
                        )
                    )
    return observations, gaps
