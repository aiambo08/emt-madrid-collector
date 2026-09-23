from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from emt_collector.bunching.domain import Event, Gap, Observation, Parameters, Passage, Route


@dataclass
class Series:
    route: Route
    observations: list[Observation]
    passages: list[Passage]
    times: list[datetime]
    gaps: list[datetime]
    breaks: list[tuple[datetime, datetime]]
    parameters: Parameters
    observation_times: list[datetime]
    passage_times: list[datetime]

    def covered(self, start: datetime, end: datetime) -> bool:
        if not self.times or end < start:
            return False
        tolerance = timedelta(seconds=self.parameters.max_gap_seconds)
        left, right = bisect_left(self.times, start), bisect_right(self.times, end)
        if left == right or self.times[left] - start > tolerance:
            return False
        if end - self.times[right - 1] > tolerance:
            return False
        gap = bisect_left(self.gaps, start)
        if gap < len(self.gaps) and self.gaps[gap] <= end:
            return False
        return not any(start <= a and b <= end for a, b in self.breaks)


def build_series(
    observations: list[Observation], gaps: list[Gap], parameters: Parameters
) -> list[Series]:
    grouped: dict[Route, list[Observation]] = defaultdict(list)
    for row in observations:
        lag = (row.available_at - row.sample_ts).total_seconds()
        if row.route.destination and 0 <= lag <= parameters.max_latency_seconds:
            grouped[row.route].append(row)
    result = []
    for route, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: (row.available_at, row.sample_ts, row.bus_id))
        times = sorted({row.available_at for row in rows})
        passages = []
        last_near: dict[int, datetime] = {}
        for row in rows:
            if (
                row.is_head
                or row.eta is None
                or not 0 <= row.eta <= parameters.near_seconds
                or row.distance_m is None
                or not 0 <= row.distance_m <= parameters.near_metres
            ):
                continue
            previous = last_near.get(row.bus_id)
            last_near[row.bus_id] = max(previous, row.sample_ts) if previous else row.sample_ts
            if (
                previous
                and (row.sample_ts - previous).total_seconds() <= parameters.bus_cooldown_seconds
            ):
                continue
            at = row.sample_ts + timedelta(seconds=row.eta)
            passages.append(Passage(route, row.bus_id, at, max(at, row.available_at)))
        passages.sort(key=lambda passage: (passage.at, passage.bus_id))
        relevant_gaps = sorted(
            gap.at
            for gap in gaps
            if (gap.stop_id is None or gap.stop_id == route.stop_id)
            and (gap.line is None or gap.line == route.line)
        )
        breaks = [
            (a, b)
            for a, b in zip(times, times[1:], strict=False)
            if (b - a).total_seconds() > parameters.max_gap_seconds
        ]
        result.append(
            Series(
                route,
                rows,
                passages,
                times,
                relevant_gaps,
                breaks,
                parameters,
                [row.available_at for row in rows],
                [passage.at for passage in passages],
            )
        )
    return result


def detect(series: Series) -> list[Event]:
    p = series.parameters
    result = []
    passages = [item for item in series.passages if item.available_at <= series.times[-1]]
    for index in range(1, len(passages)):
        previous, first = passages[index - 1 : index + 1]
        gap = (first.at - previous.at).total_seconds()
        if gap < p.gap_seconds:
            continue
        cluster: list[Passage] = []
        bus_ids: set[int] = set()
        for passage in passages[index:]:
            if (passage.at - first.at).total_seconds() > p.cluster_seconds:
                break
            if passage.bus_id not in bus_ids:
                cluster.append(passage)
                bus_ids.add(passage.bus_id)
        if len(cluster) < p.min_buses:
            continue
        last = cluster[-1]
        known_at = max(item.available_at for item in cluster)
        if not series.covered(previous.at, known_at):
            continue
        result.append(
            Event(
                series.route,
                first.at,
                last.at,
                previous.at,
                tuple(item.bus_id for item in cluster),
                gap,
                (last.at - first.at).total_seconds(),
            )
        )
    return result
