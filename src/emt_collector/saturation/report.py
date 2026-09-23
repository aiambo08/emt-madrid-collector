from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from html import escape
from importlib.resources import files
from pathlib import Path
from statistics import mean
from typing import Literal

from pydantic import BaseModel

from emt_collector.bunching.detector import Series
from emt_collector.bunching.domain import Route
from emt_collector.bunching.features import MADRID, features
from emt_collector.bunching.report import local, table
from emt_collector.saturation.domain import Headway, SaturatedHeadway, SaturationParameters, Window
from emt_collector.saturation.headways import RouteReference
from emt_collector.saturation.model import SaturationModel, Scored

TITLE = "Saturación del servicio · EMT Madrid"
SUBTITLE = (
    "Intervalos entre buses anómalos por línea, parada y hora; "
    "probabilidad de que la siguiente llegada cierre un intervalo saturado y espera prevista."
)
FOOTER = (
    "Sin datos de ocupación: la saturación se define por el intervalo entre pasos inferidos "
    "frente a la mediana histórica de la ruta y hora. Los periodos sin cobertura se excluyen."
)


class Prediction(BaseModel):
    line: str
    stop_id: str
    destination: str
    as_of: datetime
    minutes_since_last_bus: float | None
    reference_minutes: float | None
    threshold_minutes: float | None
    probability: float | None
    expected_wait_minutes: float | None
    status: str


def predict(series: list[Series], model: SaturationModel, at: datetime) -> list[Prediction]:
    if at < model.trained_through:
        raise ValueError("No se puede predecir antes del final de las etiquetas de entrenamiento.")
    by_route = {item.route: item for item in series}
    routes = sorted(set(by_route) | {Route(*key) for key in model.routes})
    thresholds = model.thresholds()
    result = []
    for route in routes:
        values = features(by_route[route], at) if route in by_route else None
        known = (route.line, route.stop_id, route.destination) in model.routes
        status = (
            "ok"
            if known and values is not None
            else ("unseen_route" if not known else "insufficient_coverage")
        )
        ok = status == "ok" and values is not None
        result.append(
            Prediction(
                line=route.line,
                stop_id=route.stop_id,
                destination=route.destination,
                as_of=at,
                minutes_since_last_bus=values[0] if values else None,
                reference_minutes=thresholds.reference_minutes(route, at) if known else None,
                threshold_minutes=thresholds.minutes(route, at) if known else None,
                probability=model.probability(route, at, values) if ok and values else None,
                expected_wait_minutes=model.expected_wait(route, at, values)
                if ok and values
                else None,
                status=status,
            )
        )
    return result


def _metric(name: str, value: int) -> str:
    return (
        f'<div class="a-metric"><div class="a-metric__label">{escape(name)}</div>'
        f'<div class="a-metric__value">{value:,}</div></div>'
    )


def _chart(kind: str, unit: str, title: str, inner: str, note: str = "") -> str:
    note_html = f'<p class="a-section__note">{note}</p>' if note else ""
    return (
        f'<figure class="a-panel" data-a-chart="{kind}" data-a-chart-unit="{escape(unit, True)}" '
        f'tabindex="0"><figcaption class="a-panel__title">{escape(title)}</figcaption>'
        f"{note_html}{inner}</figure>"
    )


def _example_day(scored: list[Scored]) -> tuple[tuple[str, str, str], datetime] | None:
    counts = Counter((row.route, row.at.astimezone(MADRID).date()) for row in scored)
    if not counts:
        return None
    (route, day), _ = counts.most_common(1)[0]
    return route, datetime(day.year, day.month, day.day, tzinfo=MADRID)


def write_outputs(
    output: Path,
    source: Literal["synthetic", "database"],
    parameters: SaturationParameters,
    series: list[Series],
    intervals: list[Headway],
    saturated: list[SaturatedHeadway],
    reference: list[RouteReference],
    reference_scope: str,
    rows: list[Window],
    model: SaturationModel | None,
    scored: list[Scored],
    predictions: list[Prediction],
    training_error: str | None,
    sample_count: int,
    start: datetime,
    end: datetime,
) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "headways.json").write_text(
        json.dumps(
            [
                {
                    "line": item.headway.route.line,
                    "stop_id": item.headway.route.stop_id,
                    "destination": item.headway.route.destination,
                    "start": item.headway.start.isoformat(),
                    "end": item.headway.end.isoformat(),
                    "previous_bus": item.headway.previous_bus,
                    "bus": item.headway.bus,
                    "minutes": round(item.headway.minutes, 2),
                    "reference_minutes": round(item.reference_minutes, 2),
                    "threshold_minutes": round(item.threshold_minutes, 2),
                }
                for item in sorted(saturated, key=lambda item: item.headway.end)
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output / "predictions.json").write_text(
        json.dumps([p.model_dump(mode="json") for p in predictions], indent=2), encoding="utf-8"
    )
    summary = {
        "source": source,
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "parameters": parameters.model_dump(),
        "observations": sample_count,
        "usable_routes": len(series),
        "inferred_passages": sum(len(s.passages) for s in series),
        "headways": len(intervals),
        "saturated_headways": len(saturated),
        "reference_scope": reference_scope,
        "labeled_windows": len(rows),
        "training_error": training_error,
        "evaluation": model.evaluation.model_dump(mode="json") if model else None,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if model:
        model.save(output / "model.json")
    with (output / "backtest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "line",
                "stop_id",
                "destination",
                "at_utc",
                "saturated",
                "probability",
                "baseline_probability",
                "wait_minutes",
                "expected_wait",
                "baseline_wait",
            ]
        )
        for row in scored:
            writer.writerow(
                [
                    *row.route,
                    row.at.isoformat(),
                    row.saturated,
                    row.probability,
                    row.baseline_probability,
                    round(row.wait_minutes, 2),
                    round(row.expected_wait, 2),
                    round(row.baseline_wait, 2),
                ]
            )
    (output / "report.html").write_text(
        _html(
            source,
            parameters,
            intervals,
            saturated,
            reference,
            reference_scope,
            rows,
            model,
            scored,
            predictions,
            training_error,
            sample_count,
            start,
            end,
        ),
        encoding="utf-8",
    )


def _html(
    source: Literal["synthetic", "database"],
    parameters: SaturationParameters,
    intervals: list[Headway],
    saturated: list[SaturatedHeadway],
    reference: list[RouteReference],
    reference_scope: str,
    rows: list[Window],
    model: SaturationModel | None,
    scored: list[Scored],
    predictions: list[Prediction],
    training_error: str | None,
    sample_count: int,
    start: datetime,
    end: datetime,
) -> str:
    label = (
        "DEMO SINTÉTICA · no son datos reales de EMT"
        if source == "synthetic"
        else "HISTÓRICO DE LA BASE DE DATOS"
    )
    body = (
        "<style>@media screen and (max-width:640px){"
        ".a-panel[data-a-chart]{overflow-x:auto}"
        ".a-panel[data-a-chart] .a-chart{min-width:640px}"
        ".a-chart,.a-chart__band{touch-action:pan-x pan-y}"
        "}</style>"
        f'<section class="a-section"><div class="a-callout a-callout--risk">{label}</div>'
        f'<p class="a-prose">Periodo: {local(start)} → {local(end)} (fin exclusivo). '
        "Horas en Europe/Madrid, incluido desplazamiento UTC.</p>"
        f'<p class="a-prose">Intervalo saturado: el tiempo entre dos pasos inferidos '
        f"consecutivos de la misma línea, parada y destino supera {parameters.ratio:g}× la "
        f"mediana de esa ruta a esa hora y, como mínimo, {parameters.min_headway_seconds / 60:g} "
        f"minutos. Referencia calculada con {escape(reference_scope)}.</p>"
        '<div class="a-grid">'
        + _metric("Observaciones", sample_count)
        + _metric("Intervalos medidos", len(intervals))
        + _metric("Intervalos saturados", len(saturated))
        + _metric("Ventanas etiquetadas", len(rows))
        + "</div></section>"
    )
    body += '<section class="a-section"><h2 class="a-section__title">Evaluación temporal</h2>'
    if model:
        ev = model.evaluation
        body += (
            '<p class="a-prose">Dos modelos lineales con las mismas features (retardo desde el '
            "último bus, intervalos recientes, ETAs, referencia horaria, calendario y ruta): "
            "regresión logística para la probabilidad de intervalo saturado y regresión ridge "
            "para los minutos de espera hasta la siguiente llegada. Entrenamiento en el 70% "
            "inicial de instantes; evaluación en el tramo final; escalado, referencia y "
            "baselines ajustados solo con entrenamiento.</p>"
            f'<p class="a-section__note">Entrenamiento: {local(ev.train_start)} → '
            f"{local(ev.train_end)}; etiquetas hasta {local(ev.train_labels_end)}. Evaluación: "
            f"{local(ev.test_start)} → {local(ev.test_end)}. {ev.purged_samples} ventanas "
            "purgadas. Umbral de clasificación: 0,5.</p>"
        )
        body += table(
            ["Modelo", "Ventanas", "Prevalencia", "Precisión", "Recall", "AP", "Brier ↓"],
            [
                [
                    name,
                    str(m.samples),
                    f"{m.prevalence:.1%}",
                    f"{m.precision:.1%}",
                    f"{m.recall:.1%}",
                    f"{m.average_precision:.3f}" if m.average_precision is not None else "N/D",
                    f"{m.brier:.3f}",
                ]
                for name, m in (
                    ("Modelo", ev.saturation),
                    ("Baseline línea/hora", ev.saturation_baseline),
                )
            ],
            "Probabilidad de que la siguiente llegada cierre un intervalo saturado",
        )
        body += table(
            ["Modelo", "Ventanas", "MAE (min) ↓", "RMSE (min) ↓", "Error mediano (min)"],
            [
                [
                    name,
                    str(m.samples),
                    f"{m.mae_minutes:.2f}",
                    f"{m.rmse_minutes:.2f}",
                    f"{m.median_error_minutes:+.2f}",
                ]
                for name, m in (("Modelo", ev.wait), ("Baseline ruta/hora", ev.wait_baseline))
            ],
            "Minutos de espera hasta la siguiente llegada (ventanas solapadas cada 5 min)",
        )
    else:
        body += f'<div class="a-empty">{escape(training_error or "Sin modelo.")}</div>'
    body += "</section>"

    example = _example_day(scored)
    if example:
        route, day = example
        day_rows = sorted(
            (
                row
                for row in scored
                if row.route == route and day <= row.at.astimezone(MADRID) < day + timedelta(days=1)
            ),
            key=lambda row: row.at,
        )
        body += (
            '<section class="a-section"><h2 class="a-section__title">Un día, paso a paso</h2>'
            f'<p class="a-prose">{escape(" / ".join(route))}, {day.strftime("%Y-%m-%d")} '
            "(tramo de evaluación). Espera prevista frente a la espera real hasta la siguiente "
            "llegada, en cada instante etiquetado.</p>"
            + _chart(
                "line",
                " min",
                "Espera prevista y real",
                table(
                    ["Hora (Madrid)", "Espera prevista", "Espera real", "Baseline ruta/hora"],
                    [
                        [
                            row.at.astimezone(MADRID).strftime("%H:%M"),
                            f"{row.expected_wait:.1f}",
                            f"{row.wait_minutes:.1f}",
                            f"{row.baseline_wait:.1f}",
                        ]
                        for row in day_rows
                    ],
                    "Minutos hasta la siguiente llegada",
                ),
                "En pantallas estrechas, desplaza el gráfico horizontalmente o abre su tabla.",
            )
            + _chart(
                "bar",
                "%",
                "Probabilidad de intervalo saturado",
                table(
                    ["Hora (Madrid)", "Probabilidad", "Etiqueta (0 o 100%)"],
                    [
                        [
                            row.at.astimezone(MADRID).strftime("%H:%M"),
                            f"{row.probability * 100:.1f}%",
                            f"{row.saturated * 100}%",
                        ]
                        for row in day_rows
                    ],
                    "La etiqueta indica si la siguiente llegada real cerró un intervalo saturado",
                ),
            )
            + "</section>"
        )

    groups: dict[tuple[str, int], list[Scored]] = defaultdict(list)
    for row in scored:
        groups[(row.route[0], row.at.astimezone(MADRID).hour)].append(row)
    body += (
        '<section class="a-section"><h2 class="a-section__title">Saturación por línea y hora</h2>'
        '<p class="a-section__note">Media de probabilidades por ventana y parada en el tramo de '
        "evaluación frente a la fracción de etiquetas positivas y espera real media. No es una "
        "previsión para mañana.</p>"
    )
    for line in sorted({key[0] for key in groups}):
        body += _chart(
            "bar",
            "%",
            f"Línea {line}: probabilidad de saturación",
            table(
                ["Hora", "Probabilidad estimada", "Frecuencia observada"],
                [
                    [
                        f"{hour:02}:00",
                        f"{mean(r.probability for r in group) * 100:.1f}%",
                        f"{mean(r.saturated for r in group) * 100:.1f}%",
                    ]
                    for (group_line, hour), group in sorted(groups.items())
                    if group_line == line
                ],
                f"Línea {line}",
            ),
        )
        body += _chart(
            "bar",
            " min",
            f"Línea {line}: espera hasta la siguiente llegada",
            table(
                ["Hora", "Espera prevista", "Espera real"],
                [
                    [
                        f"{hour:02}:00",
                        f"{mean(r.expected_wait for r in group):.1f}",
                        f"{mean(r.wait_minutes for r in group):.1f}",
                    ]
                    for (group_line, hour), group in sorted(groups.items())
                    if group_line == line
                ],
                f"Línea {line}",
            ),
        )
    body += "</section>"

    body += (
        '<section class="a-section"><h2 class="a-section__title">Intervalo de referencia</h2>'
        '<p class="a-section__note">Mediana del intervalo entre buses por hora local de la '
        "llegada; con menos de 5 intervalos en una hora se usa la mediana de la ruta. El umbral "
        f"es {parameters.ratio:g}× la referencia con mínimo "
        f"{parameters.min_headway_seconds / 60:g} min.</p>"
    )
    floor = parameters.min_headway_seconds / 60
    for item in reference:
        body += _chart(
            "bar",
            " min",
            f"{item.route.key} ({item.samples} intervalos)",
            table(
                ["Hora", "Referencia", "Umbral"],
                [
                    [
                        f"{hour:02}:00",
                        f"{item.hourly_minutes[hour]:.1f}",
                        f"{max(item.hourly_minutes[hour] * parameters.ratio, floor):.1f}",
                    ]
                    for hour in range(24)
                ],
                item.route.key,
            ),
        )
    if not reference:
        body += '<div class="a-empty">Sin intervalos suficientes para calcular referencias.</div>'
    body += "</section>"

    body += (
        '<section class="a-section"><h2 class="a-section__title">Última predicción</h2>'
        '<p class="a-section__note">Probabilidad de que la siguiente llegada cierre un intervalo '
        "saturado y minutos de espera previstos desde el instante indicado. Sin cobertura o ruta "
        "no entrenada se devuelve N/D.</p>"
        + table(
            [
                "Línea",
                "Parada",
                "Destino",
                "Instante",
                "Desde último bus",
                "Umbral",
                "P(saturado)",
                "Espera prevista",
                "Estado",
            ],
            [
                [
                    p.line,
                    p.stop_id,
                    p.destination,
                    local(p.as_of),
                    f"{p.minutes_since_last_bus:.1f} min"
                    if p.minutes_since_last_bus is not None
                    else "N/D",
                    f"{p.threshold_minutes:.1f} min" if p.threshold_minutes is not None else "N/D",
                    f"{p.probability:.1%}" if p.probability is not None else "N/D",
                    f"{p.expected_wait_minutes:.1f} min"
                    if p.expected_wait_minutes is not None
                    else "N/D",
                    p.status,
                ]
                for p in predictions
            ],
            "Predicciones al final del periodo analizado",
        )
        + "</section>"
    )

    body += (
        '<section class="a-section"><h2 class="a-section__title">Intervalos saturados</h2>'
        '<p class="a-section__note">Hasta 200 intervalos recientes; headways.json contiene todos. '
        "Los tiempos son pasos inferidos de las estimaciones de llegada, no confirmados por "
        "sensores.</p>"
        '<label for="line-filter">Filtrar línea</label> '
        '<select id="line-filter" data-a-filter="#saturated" data-a-filter-key="line">'
        '<option value="all">Todas</option>'
        + "".join(
            f'<option value="{escape(line, quote=True)}">{escape(line)}</option>'
            for line in sorted({item.headway.route.line for item in saturated})
        )
        + "</select>"
        + table(
            [
                "Línea",
                "Parada",
                "Destino",
                "Llegada",
                "Intervalo (min)",
                "Referencia (min)",
                "Umbral (min)",
                "Buses",
            ],
            [
                [
                    item.headway.route.line,
                    item.headway.route.stop_id,
                    item.headway.route.destination,
                    local(item.headway.end),
                    f"{item.headway.minutes:.1f}",
                    f"{item.reference_minutes:.1f}",
                    f"{item.threshold_minutes:.1f}",
                    f"{item.headway.previous_bus} → {item.headway.bus}",
                ]
                for item in sorted(saturated, key=lambda item: item.headway.end, reverse=True)[:200]
            ],
            "Intervalos por parada y destino",
            "saturated",
        )
        + "</section>"
    )
    template = files("emt_collector.bunching").joinpath("report_template.html").read_text("utf-8")
    for old, new in (
        ("Bus bunching · EMT Madrid", TITLE),
        (
            "Detección de agrupamientos y riesgo de inicio de un episodio en el próximo horizonte.",
            SUBTITLE,
        ),
        (
            "Pasos inferidos a partir de estimaciones de llegada. Los periodos sin cobertura se "
            "excluyen; las probabilidades requieren validación con pasos reales.",
            FOOTER,
        ),
        ('lang="en"', 'lang="es"'),
    ):
        if old not in template:
            raise ValueError(f"Plantilla de informe sin el texto esperado: {old[:40]}…")
        template = template.replace(old, escape(new) if new != 'lang="es"' else new)
    return template.replace("<!--BUNCHING_CONTENT-->", body)
