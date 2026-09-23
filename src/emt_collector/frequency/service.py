from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev

from emt_collector.bunching.detector import Series
from emt_collector.bunching.features import MADRID
from emt_collector.frequency.domain import FrequencyParameters, HourlyService
from emt_collector.saturation.domain import Headway
from emt_collector.saturation.headways import Threshold, hour_of

Demand = dict[tuple[str | None, int], float]


def cycle_minutes(series: Series, parameters: FrequencyParameters) -> tuple[float | None, int]:
    """Mediana del tiempo que tarda el mismo bus en volver a pasar por la parada en el mismo
    sentido: aproxima el tiempo de ciclo (ida y vuelta más regulación) de la ruta."""
    by_bus: dict[int, list[datetime]] = defaultdict(list)
    for passage in series.passages:
        by_bus[passage.bus_id].append(passage.at)
    values = []
    for times in by_bus.values():
        times.sort()
        for previous, current in zip(times, times[1:], strict=False):
            minutes = (current - previous).total_seconds() / 60
            if parameters.min_cycle_minutes <= minutes <= parameters.max_cycle_minutes:
                values.append(minutes)
    if not values:
        return None, 0
    return median(values), len(values)


def read_demand_csv(path: Path) -> Demand:
    """CSV con columnas `hour,weight` y opcionalmente `line`; sin línea aplica a todas."""
    result: Demand = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"hour", "weight"} <= set(reader.fieldnames):
            raise ValueError("El CSV de demanda necesita las columnas hour y weight.")
        for row in reader:
            hour = int(row["hour"])
            weight = float(row["weight"])
            if not 0 <= hour <= 23 or weight < 0:
                raise ValueError(f"Fila de demanda inválida: hora {hour}, peso {weight}.")
            line = (row.get("line") or "").strip() or None
            result[(line, hour)] = weight
    if not result:
        raise ValueError("El CSV de demanda está vacío.")
    return result


def hourly_service(
    series: Series,
    intervals: list[Headway],
    threshold: Threshold,
    parameters: FrequencyParameters,
    demand: Demand | None,
) -> list[HourlyService]:
    """Una fila por hora local con al menos `min_hour_samples` intervalos completos.

    Peso de demanda: `csv` toma el perfil aportado (fila de la línea antes que la general);
    `uniform` vale 1; `proxy` usa los buses observados por día multiplicados por
    (1 + tasa de intervalos saturados): la oferta que el operador ya programa, corregida
    al alza donde el servicio se degrada."""
    days = {t.astimezone(MADRID).date() for t in series.times}
    if len(days) < parameters.min_days:
        return []
    by_hour: dict[int, list[Headway]] = defaultdict(list)
    for item in intervals:
        by_hour[hour_of(item.end)].append(item)
    result = []
    for hour in range(parameters.service_start_hour, parameters.service_end_hour):
        rows = by_hour.get(hour, [])
        if len(rows) < parameters.min_hour_samples:
            continue
        minutes = [item.minutes for item in rows]
        avg = mean(minutes)
        cv = pstdev(minutes) / avg
        saturated = sum(1 for item in rows if threshold.is_saturated(item))
        hour_days = len({item.end.astimezone(MADRID).date() for item in rows})
        if demand is not None:
            weight = demand.get((series.route.line, hour), demand.get((None, hour), 0.0))
        elif parameters.demand_mode == "uniform":
            weight = 1.0
        else:
            weight = len(rows) / hour_days * (1 + saturated / len(rows))
        result.append(
            HourlyService(series.route, hour, len(rows), hour_days, avg, cv, saturated, weight)
        )
    return result
