from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

from emt_collector.bunching.detector import Series
from emt_collector.bunching.domain import Event, Example

MADRID = ZoneInfo("Europe/Madrid")
FEATURE_NAMES = (
    "minutes_since_passage",
    "last_headway_minutes",
    "mean_headway_minutes",
    "std_headway_minutes",
    "passages_last_hour",
    "next_eta_minutes",
    "eta_spread_minutes",
    "approaching_buses",
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
    "weekend",
)


def features(series: Series, at: datetime) -> tuple[float, ...] | None:
    p = series.parameters
    start = at - timedelta(seconds=p.lookback_seconds)
    if not series.covered(start, at):
        return None
    past = [
        passage
        for passage in series.passages[
            bisect_left(series.passage_times, start) : bisect_right(series.passage_times, at)
        ]
        if start <= passage.at <= at and passage.available_at <= at
    ]
    if len(past) < 2:
        return None
    headways = [(b.at - a.at).total_seconds() / 60 for a, b in zip(past, past[1:], strict=False)]
    available = series.observations[
        bisect_left(series.observation_times, start) : bisect_right(series.observation_times, at)
    ]
    if not available:
        return None
    latest_sample = max(row.sample_ts for row in available)
    estimates = {
        row.bus_id: max(0.0, row.eta - (at - row.sample_ts).total_seconds()) / 60
        for row in available
        if row.sample_ts == latest_sample
        and row.eta is not None
        and 0 <= row.eta < 999_999
        and not row.is_head
    }
    etas = sorted(estimates.values())[:3]
    local = at.astimezone(MADRID)
    hour = (local.hour + local.minute / 60) * 2 * math.pi / 24
    weekday = local.weekday() * 2 * math.pi / 7
    return (
        (at - past[-1].at).total_seconds() / 60,
        headways[-1],
        mean(headways),
        pstdev(headways),
        float(len(past)),
        etas[0] if etas else p.horizon_seconds / 60,
        etas[-1] - etas[0] if len(etas) > 1 else 0.0,
        float(len(etas)),
        math.sin(hour),
        math.cos(hour),
        math.sin(weekday),
        math.cos(weekday),
        float(local.weekday() >= 5),
    )


def examples(series: Series, events: list[Event], end: datetime) -> list[Example]:
    p = series.parameters
    if not series.times:
        return []
    first = series.times[0] + timedelta(seconds=p.lookback_seconds)
    at = datetime.fromtimestamp(
        math.ceil(first.timestamp() / p.step_seconds) * p.step_seconds, tz=first.tzinfo
    )
    horizon = timedelta(seconds=p.horizon_seconds)
    confirmation = timedelta(seconds=p.cluster_seconds + p.near_seconds + p.max_latency_seconds)
    last = min(end, series.times[-1])
    result = []
    while at + horizon + confirmation <= last:
        label_end = at + horizon + confirmation
        values = features(series, at)
        if values is not None and series.covered(at, label_end):
            target = int(any(at < event.start <= at + horizon for event in events))
            result.append(Example(series.route, at, label_end, values, target))
        at += timedelta(seconds=p.step_seconds)
    return result
