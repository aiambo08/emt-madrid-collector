from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from html import escape
from importlib.resources import files
from pathlib import Path
from statistics import mean
from typing import Literal

from pydantic import BaseModel

from emt_collector.bunching.detector import Series
from emt_collector.bunching.domain import Event, Example, Parameters, Route
from emt_collector.bunching.features import MADRID, features
from emt_collector.bunching.model import ForecastModel


class Prediction(BaseModel):
    line: str
    stop_id: str
    destination: str
    as_of: datetime
    until: datetime
    probability: float | None
    status: str


def predict(series: list[Series], model: ForecastModel, at: datetime) -> list[Prediction]:
    if at < model.trained_through:
        raise ValueError("No se puede predecir antes del final de las etiquetas de entrenamiento.")
    by_route = {item.route: item for item in series}
    routes = sorted(set(by_route) | {Route(*key) for key in model.routes})
    result = []
    for route in routes:
        values = features(by_route[route], at) if route in by_route else None
        known = (route.line, route.stop_id, route.destination) in model.routes
        status = (
            "ok"
            if known and values is not None
            else ("unseen_route" if not known else "insufficient_coverage")
        )
        probability = model.probability(route, values) if status == "ok" and values else None
        result.append(
            Prediction(
                line=route.line,
                stop_id=route.stop_id,
                destination=route.destination,
                as_of=at,
                until=at + timedelta(seconds=model.parameters.horizon_seconds),
                probability=probability,
                status=status,
            )
        )
    return result


def table(headers: list[str], rows: list[list[str]], caption: str, ident: str = "") -> str:
    head = "".join(f'<th scope="col">{escape(cell)}</th>' for cell in headers)
    body = "".join(
        f'<tr data-line="{escape(row[0], quote=True)}">'
        + "".join(f"<td>{escape(cell)}</td>" for cell in row)
        + "</tr>"
        for row in rows
    )
    identity = f' id="{escape(ident, quote=True)}"' if ident else ""
    return (
        f'<div class="a-table-scroll"><table class="a-table"{identity}>'
        f"<caption>{escape(caption)}</caption><thead><tr>{head}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def local(value: datetime) -> str:
    return value.astimezone(MADRID).strftime("%Y-%m-%d %H:%M %z")


def write_outputs(
    output: Path,
    source: Literal["synthetic", "database"],
    parameters: Parameters,
    series: list[Series],
    events: list[Event],
    rows: list[Example],
    model: ForecastModel | None,
    holdout: list[tuple[Example, float, float]],
    predictions: list[Prediction],
    training_error: str | None,
    sample_count: int,
    start: datetime,
    end: datetime,
) -> None:
    output.mkdir(parents=True, exist_ok=False)
    event_data = [asdict(event) for event in sorted(events, key=lambda event: event.start)]
    (output / "events.json").write_text(
        json.dumps(event_data, default=str, ensure_ascii=False, indent=2), encoding="utf-8"
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
        "events": len(events),
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
        writer.writerow(["line", "stop_id", "destination", "at_utc", "label", "risk", "baseline"])
        for row, risk, naive in holdout:
            writer.writerow(
                [
                    row.route.line,
                    row.route.stop_id,
                    row.route.destination,
                    row.at.isoformat(),
                    row.target,
                    risk,
                    naive,
                ]
            )
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
        f'<p class="a-prose">Episodio: ≥{parameters.min_buses} buses distintos, misma línea, '
        f"parada y destino; separación total ≤{parameters.cluster_seconds / 60:g} min, "
        f"tras ≥{parameters.gap_seconds / 60:g} min entre pasos inferidos. "
        "Una incidencia puede aparecer en varias paradas.</p>"
        '<div class="a-grid">'
        + "".join(
            f'<div class="a-metric"><div class="a-metric__label">{name}</div>'
            f'<div class="a-metric__value">{value:,}</div></div>'
            for name, value in (
                ("Observaciones", sample_count),
                ("Episodios inferidos", len(events)),
                ("Ventanas etiquetadas", len(rows)),
            )
        )
        + "</div></section>"
    )
    body += '<section class="a-section"><h2 class="a-section__title">Evaluación temporal</h2>'
    if model:
        evaluation = model.evaluation
        body += (
            '<p class="a-prose">Regresión logística con retardos, intervalos entre buses, '
            "ETAs y calendario. Entrenamiento en el 70% inicial de instantes; evaluación "
            "en el tramo final. Escalado y baseline ajustados solo con entrenamiento.</p>"
            f'<p class="a-section__note">Entrenamiento: {local(evaluation.train_start)} → '
            f"{local(evaluation.train_end)}; etiquetas hasta {local(evaluation.train_labels_end)}. "
            f"Evaluación: {local(evaluation.test_start)} → {local(evaluation.test_end)}. "
            f"{evaluation.purged_samples} ventanas purgadas. Umbral: 0,5.</p>"
        )
        metric_rows = []
        for name, metrics in (
            ("Modelo", evaluation.model),
            ("Baseline línea/hora", evaluation.baseline),
        ):
            metric_rows.append(
                [
                    name,
                    str(metrics.samples),
                    f"{metrics.prevalence:.1%}",
                    f"{metrics.precision:.1%}",
                    f"{metrics.recall:.1%}",
                    f"{metrics.average_precision:.3f}"
                    if metrics.average_precision is not None
                    else "N/D",
                    f"{metrics.brier:.3f}",
                ]
            )
        body += table(
            ["Modelo", "Ventanas", "Prevalencia", "Precisión", "Recall", "AP", "Brier ↓"],
            metric_rows,
            "Métricas sobre ventanas de evaluación (solapadas; no episodios independientes)",
        )
    else:
        body += f'<div class="a-empty">{escape(training_error or "Sin modelo.")}</div>'
    body += "</section>"
    example = next(
        (
            event
            for event in sorted(events, key=lambda item: item.start)
            if model
            and model.evaluation.test_start + timedelta(minutes=30)
            <= event.start
            <= model.evaluation.test_end - timedelta(minutes=15)
        ),
        None,
    )
    if example:
        body += (
            '<section class="a-section"><h2 class="a-section__title">Un episodio, paso a paso</h2>'
            f'<p class="a-prose">{escape(example.route.key)}. Paso anterior: '
            f"{local(example.previous_passage)}. Primer bus del grupo: {local(example.start)}. "
            f"{len(example.bus_ids)} buses en {example.span_seconds / 60:.1f} min después de "
            f"{example.gap_seconds / 60:.1f} min de intervalo.</p>"
            '<figure class="a-panel" data-a-chart="line" data-a-chart-unit="%" tabindex="0">'
            '<figcaption class="a-panel__title">Riesgo antes y después del episodio</figcaption>'
            '<p class="a-section__note">Hora de Madrid: '
            f"{local(example.start - timedelta(minutes=30))} → "
            f"{local(example.start + timedelta(minutes=15))}.</p>"
            '<p class="a-section__note">En pantallas estrechas, desplaza el gráfico '
            "horizontalmente o abre su tabla de datos.</p>"
            + table(
                ["Hora (Madrid)", "Probabilidad", "Etiqueta (0 o 100%)"],
                [
                    [
                        row.at.astimezone(MADRID).strftime("%H:%M"),
                        f"{risk * 100:.1f}%",
                        f"{row.target * 100}%",
                    ]
                    for row, risk, _ in holdout
                    if row.route == example.route
                    and example.start - timedelta(minutes=30)
                    <= row.at
                    <= example.start + timedelta(minutes=15)
                ],
                "La etiqueta indica inicio en el próximo horizonte, no presencia actual del grupo",
            )
            + "</figure></section>"
        )
    groups: dict[tuple[str, int], list[tuple[float, int]]] = defaultdict(list)
    for row, risk, _ in holdout:
        groups[(row.route.line, row.at.astimezone(MADRID).hour)].append((risk, row.target))
    body += (
        '<section class="a-section"><h2 class="a-section__title">Riesgo por línea y hora</h2>'
        '<p class="a-section__note">Media de probabilidades por ventana y parada en el tramo '
        "de evaluación, comparada con la fracción de etiquetas positivas. No es una previsión "
        "para mañana ni la probabilidad conjunta de toda una línea.</p>"
    )
    for line in sorted({key[0] for key in groups}):
        chart_rows = [
            [
                f"{hour:02}:00",
                f"{mean(x[0] for x in group) * 100:.1f}%",
                f"{mean(x[1] for x in group) * 100:.1f}%",
            ]
            for (group_line, hour), group in sorted(groups.items())
            if group_line == line
        ]
        body += (
            '<figure class="a-panel" data-a-chart="bar" data-a-chart-unit="%" tabindex="0">'
            f'<figcaption class="a-panel__title">Línea {escape(line)}</figcaption>'
            + table(
                ["Hora", "Riesgo estimado", "Frecuencia observada"], chart_rows, f"Línea {line}"
            )
            + "</figure>"
        )
    body += (
        '</section><section class="a-section"><h2 class="a-section__title">Última predicción</h2>'
    )
    body += (
        f'<p class="a-section__note">Inicio de un nuevo episodio en los siguientes '
        f"{parameters.horizon_seconds / 60:g} minutos. Riesgo sin calibración externa; "
        "sin cobertura o ruta no entrenada se devuelve N/D.</p>"
        + table(
            ["Línea", "Parada", "Destino", "Desde", "Hasta", "Riesgo", "Estado"],
            [
                [
                    p.line,
                    p.stop_id,
                    p.destination,
                    local(p.as_of),
                    local(p.until),
                    f"{p.probability:.1%}" if p.probability is not None else "N/D",
                    p.status,
                ]
                for p in predictions
            ],
            "Predicciones al final del periodo analizado",
        )
        + "</section>"
    )
    body += (
        '<section class="a-section"><h2 class="a-section__title">Episodios detectados</h2>'
        '<p class="a-section__note">Hasta 200 episodios recientes. events.json contiene todos. '
        "Los tiempos son estimaciones, no pasos confirmados por sensores.</p>"
        '<label for="line-filter">Filtrar línea</label> '
        '<select id="line-filter" data-a-filter="#episodes" data-a-filter-key="line">'
        '<option value="all">Todas</option>'
        + "".join(
            f'<option value="{escape(line, quote=True)}">{escape(line)}</option>'
            for line in sorted({event.route.line for event in events})
        )
        + "</select>"
        + table(
            ["Línea", "Parada", "Destino", "Primer bus", "Buses", "Hueco (min)", "Grupo (min)"],
            [
                [
                    e.route.line,
                    e.route.stop_id,
                    e.route.destination,
                    local(e.start),
                    ", ".join(str(bus) for bus in e.bus_ids),
                    f"{e.gap_seconds / 60:.1f}",
                    f"{e.span_seconds / 60:.1f}",
                ]
                for e in sorted(events, key=lambda event: event.start, reverse=True)[:200]
            ],
            "Episodios por parada y destino",
            "episodes",
        )
        + "</section>"
    )
    template = files("emt_collector.bunching").joinpath("report_template.html").read_text("utf-8")
    (output / "report.html").write_text(
        template.replace("<!--BUNCHING_CONTENT-->", body).replace('lang="en"', 'lang="es"'),
        encoding="utf-8",
    )
