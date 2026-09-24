from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from scipy.stats import mannwhitneyu

from emt_collector.bunching.detector import Series, detect
from emt_collector.bunching.domain import Event, Route
from emt_collector.bunching.features import MADRID
from emt_collector.impact.domain import (
    METRICS,
    Change,
    DifferenceInDifferences,
    ImpactParameters,
    Period,
    RouteImpact,
    Skipped,
    WindowMetrics,
)
from emt_collector.saturation.domain import Headway
from emt_collector.saturation.headways import Threshold, build_reference, headways, hour_of

Floats = NDArray[np.float64]


def window_metrics(
    series: Series,
    intervals: list[Headway],
    events: list[Event],
    threshold: Threshold,
    period: Period,
    start: datetime,
    end: datetime,
) -> WindowMetrics:
    """Intervalos cuyo segundo paso cae en la ventana y episodios que empiezan en ella; los
    días son los días locales con alguna muestra del recolector dentro de la ventana."""
    rows = [item for item in intervals if start <= item.end < end]
    flags = tuple(bool(threshold.is_saturated(item)) for item in rows)
    days = sorted({t.astimezone(MADRID).date() for t in series.times if start <= t < end})
    counts = {day: 0 for day in days}
    for event in events:
        if start <= event.start < end:
            day = event.start.astimezone(MADRID).date()
            counts[day] = counts.get(day, 0) + 1
    by_hour: dict[int, list[float]] = defaultdict(list)
    for item in rows:
        by_hour[hour_of(item.end)].append(item.minutes)
    return WindowMetrics(
        series.route,
        period,
        start,
        end,
        tuple(item.minutes for item in rows),
        flags,
        tuple(counts[day] for day in sorted(counts)),
        {hour: sum(values) / len(values) for hour, values in sorted(by_hour.items())},
    )


def _resampled(rng: np.random.Generator, metrics: WindowMetrics, samples: int) -> dict[str, Floats]:
    """Valor de cada métrica en `samples` remuestreos bootstrap de la ventana: intervalos con
    su etiqueta de saturación (pareados) y días con su número de episodios."""
    h = np.asarray(metrics.headway_minutes, dtype=np.float64)
    s = np.asarray(metrics.saturated, dtype=np.float64)
    e = np.asarray(metrics.episodes_by_day, dtype=np.float64)
    idx = rng.integers(0, len(h), size=(samples, len(h)))
    hh, ss = h[idx], s[idx]
    days = rng.integers(0, len(e), size=(samples, len(e)))
    return {
        "mean_headway_minutes": hh.mean(axis=1),
        "expected_wait_minutes": (hh * hh).sum(axis=1) / (2 * hh.sum(axis=1)),
        "saturation_rate": ss.mean(axis=1),
        "episodes_per_day": e[days].mean(axis=1),
    }


def _interval(values: Floats, confidence: float) -> tuple[float, float]:
    tail = (1 - confidence) / 2 * 100
    low, high = np.percentile(values, [tail, 100 - tail])
    return float(low), float(high)


def compare(
    before: WindowMetrics,
    after: WindowMetrics,
    parameters: ImpactParameters,
    rng: np.random.Generator,
) -> tuple[tuple[Change, ...], dict[str, Floats]]:
    """Cambios después − antes con IC bootstrap percentil; el contraste de Mann-Whitney
    compara las distribuciones completas de intervalos."""
    boot_after = _resampled(rng, after, parameters.bootstrap_samples)
    boot_before = _resampled(rng, before, parameters.bootstrap_samples)
    deltas = {name: boot_after[name] - boot_before[name] for name in METRICS}
    p_value = float(
        mannwhitneyu(before.headway_minutes, after.headway_minutes, alternative="two-sided").pvalue
    )
    changes = []
    for name in METRICS:
        low, high = _interval(deltas[name], parameters.confidence)
        changes.append(
            Change(
                name,
                before.metric(name),
                after.metric(name),
                low,
                high,
                p_value if name == "mean_headway_minutes" else None,
            )
        )
    return tuple(changes), deltas


def difference_in_differences(
    impacts: list[RouteImpact],
    deltas: dict[Route, dict[str, Floats]],
    parameters: ImpactParameters,
) -> list[DifferenceInDifferences]:
    """Media de los cambios de las rutas tratadas menos la de las rutas de control; el IC
    combina los remuestreos bootstrap de cada ruta."""
    treated = [i for i in impacts if i.role == "treated"]
    control = [i for i in impacts if i.role == "control"]
    if not treated or not control:
        return []
    result = []
    for name in METRICS:
        t_delta = sum(i.change(name).delta for i in treated) / len(treated)
        c_delta = sum(i.change(name).delta for i in control) / len(control)
        t_boot = np.mean([deltas[i.route][name] for i in treated], axis=0)
        c_boot = np.mean([deltas[i.route][name] for i in control], axis=0)
        low, high = _interval(t_boot - c_boot, parameters.confidence)
        result.append(DifferenceInDifferences(name, t_delta, c_delta, low, high))
    return result


def analyze(
    series: list[Series],
    event: datetime,
    before_start: datetime,
    after_end: datetime,
    parameters: ImpactParameters,
    role_of: Callable[[Route], Literal["treated", "control"]],
) -> tuple[list[RouteImpact], list[DifferenceInDifferences], list[Skipped]]:
    """El umbral de saturación se calibra sólo con la ventana anterior al evento, de modo que
    la tasa posterior se mide contra el servicio habitual previo."""
    if not before_start < event < after_end:
        raise ValueError("El evento debe quedar dentro del periodo analizado.")
    intervals = {item.route: headways(item) for item in series}
    baseline = [row for rows in intervals.values() for row in rows if row.end < event]
    threshold = Threshold(build_reference(baseline), parameters)
    rng = np.random.default_rng(parameters.seed)
    impacts: list[RouteImpact] = []
    skipped: list[Skipped] = []
    deltas: dict[Route, dict[str, Floats]] = {}
    for item in series:
        events = detect(item)
        before = window_metrics(
            item, intervals[item.route], events, threshold, "before", before_start, event
        )
        after = window_metrics(
            item, intervals[item.route], events, threshold, "after", event, after_end
        )
        reason = _insufficient(before, after, parameters)
        if reason:
            skipped.append(
                Skipped(
                    line=item.route.line,
                    stop_id=item.route.stop_id,
                    destination=item.route.destination,
                    reason=reason,
                )
            )
            continue
        changes, boot = compare(before, after, parameters, rng)
        deltas[item.route] = boot
        impacts.append(RouteImpact(item.route, before, after, changes, role_of(item.route)))
    return impacts, difference_in_differences(impacts, deltas, parameters), skipped


def _insufficient(
    before: WindowMetrics, after: WindowMetrics, parameters: ImpactParameters
) -> str | None:
    for window in (before, after):
        label = "antes" if window.period == "before" else "después"
        if window.headways < parameters.min_headways:
            return (
                f"{window.headways} intervalos completos {label} del evento "
                f"(mínimo {parameters.min_headways})"
            )
        if window.days < parameters.min_days:
            return f"{window.days} días con datos {label} del evento (mínimo {parameters.min_days})"
    return None


def clip_windows(
    event: datetime, before_days: int, after_days: int, now: datetime
) -> tuple[datetime, datetime]:
    """Ventana posterior recortada al presente para no contar tiempo aún no recolectado."""
    if before_days < 1 or after_days < 1:
        raise ValueError("Las ventanas deben durar al menos un día.")
    if event >= now:
        raise ValueError("El evento debe ser anterior al momento actual.")
    return event - timedelta(days=before_days), min(event + timedelta(days=after_days), now)
