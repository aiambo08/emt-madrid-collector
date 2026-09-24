from __future__ import annotations

import csv
import json
from datetime import datetime
from html import escape
from importlib.resources import files
from pathlib import Path
from typing import Literal

from emt_collector.bunching.report import local, table
from emt_collector.impact.domain import (
    METRIC_LABELS,
    METRICS,
    Change,
    DifferenceInDifferences,
    ImpactParameters,
    RouteImpact,
    Skipped,
)
from emt_collector.saturation.report import _chart, _metric

TITLE = "Análisis de impacto · EMT Madrid"
SUBTITLE = (
    "Servicio observado antes y después de un evento por línea, parada y destino, "
    "con intervalos de confianza y rutas de control."
)
FOOTER = (
    "Pasos inferidos a partir de estimaciones de llegada; los periodos sin cobertura se "
    "excluyen. Un cambio significativo no implica causalidad: sin rutas de control puede "
    "deberse a factores ajenos al evento (clima, calendario, obras en toda la red)."
)
ROLE_LABEL = {"treated": "tratada", "control": "control"}


def fmt(metric: str, value: float) -> str:
    return f"{value:.1%}" if metric == "saturation_rate" else f"{value:.2f}"


def fmt_delta(metric: str, value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1%}" if metric == "saturation_rate" else f"{sign}{value:.2f}"


def write_outputs(
    output: Path,
    source: Literal["synthetic", "database"],
    parameters: ImpactParameters,
    event: datetime,
    start: datetime,
    end: datetime,
    impacts: list[RouteImpact],
    did: list[DifferenceInDifferences],
    skipped: list[Skipped],
    sample_count: int,
) -> None:
    output.mkdir(parents=True, exist_ok=False)
    with (output / "changes.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "line",
                "stop_id",
                "destination",
                "role",
                "metric",
                "before",
                "after",
                "delta",
                "ci_low",
                "ci_high",
                "p_value",
            ]
        )
        for impact in impacts:
            for change in impact.changes:
                writer.writerow(
                    [
                        impact.route.line,
                        impact.route.stop_id,
                        impact.route.destination,
                        impact.role,
                        change.metric,
                        round(change.before, 4),
                        round(change.after, 4),
                        round(change.delta, 4),
                        round(change.ci_low, 4),
                        round(change.ci_high, 4),
                        "" if change.p_value is None else round(change.p_value, 5),
                    ]
                )
    summary = {
        "source": source,
        "event": event.isoformat(),
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "parameters": parameters.model_dump(),
        "observations": sample_count,
        "routes": [
            {
                "line": i.route.line,
                "stop_id": i.route.stop_id,
                "destination": i.route.destination,
                "role": i.role,
                "before": _window_summary(i, "before"),
                "after": _window_summary(i, "after"),
                "changes": [
                    {
                        "metric": c.metric,
                        "before": round(c.before, 4),
                        "after": round(c.after, 4),
                        "delta": round(c.delta, 4),
                        "ci_low": round(c.ci_low, 4),
                        "ci_high": round(c.ci_high, 4),
                        "p_value": None if c.p_value is None else round(c.p_value, 5),
                        "significant": c.significant,
                    }
                    for c in i.changes
                ],
            }
            for i in impacts
        ],
        "difference_in_differences": [
            {
                "metric": d.metric,
                "treated_delta": round(d.treated_delta, 4),
                "control_delta": round(d.control_delta, 4),
                "estimate": round(d.estimate, 4),
                "ci_low": round(d.ci_low, 4),
                "ci_high": round(d.ci_high, 4),
                "significant": d.significant,
            }
            for d in did
        ],
        "skipped": [item.model_dump() for item in skipped],
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.html").write_text(
        _html(source, parameters, event, start, end, impacts, did, skipped, sample_count),
        encoding="utf-8",
    )


def _window_summary(impact: RouteImpact, period: str) -> dict[str, float | int]:
    window = impact.before if period == "before" else impact.after
    return {
        "headways": window.headways,
        "days": window.days,
        "episodes": window.episodes,
        "mean_headway_minutes": round(window.mean_headway_minutes, 3),
        "cv": round(window.cv, 4),
        "expected_wait_minutes": round(window.expected_wait_minutes, 3),
        "saturation_rate": round(window.saturation_rate, 4),
    }


def _change_row(change: Change) -> list[str]:
    ci = f"[{fmt_delta(change.metric, change.ci_low)}, {fmt_delta(change.metric, change.ci_high)}]"
    return [
        METRIC_LABELS[change.metric],
        fmt(change.metric, change.before),
        fmt(change.metric, change.after),
        fmt_delta(change.metric, change.delta),
        ci,
        "" if change.p_value is None else f"{change.p_value:.3g}",
        "sí" if change.significant else "no",
    ]


def _html(
    source: Literal["synthetic", "database"],
    parameters: ImpactParameters,
    event: datetime,
    start: datetime,
    end: datetime,
    impacts: list[RouteImpact],
    did: list[DifferenceInDifferences],
    skipped: list[Skipped],
    sample_count: int,
) -> str:
    label = (
        "DEMO SINTÉTICA · no son datos reales de EMT"
        if source == "synthetic"
        else "HISTÓRICO DE LA BASE DE DATOS"
    )
    treated = [i for i in impacts if i.role == "treated"]
    control = [i for i in impacts if i.role == "control"]
    confidence = f"{parameters.confidence:.0%}"
    body = (
        "<style>@media screen and (max-width:640px){"
        ".a-panel[data-a-chart]{overflow-x:auto}"
        ".a-panel[data-a-chart] .a-chart{min-width:640px}"
        ".a-chart,.a-chart__band{touch-action:pan-x pan-y}"
        "}</style>"
        f'<section class="a-section"><div class="a-callout a-callout--risk">{label}</div>'
        f'<p class="a-prose">Evento: <b>{local(event)}</b>. Antes: {local(start)} → '
        f"{local(event)}; después: {local(event)} → {local(end)} (fin exclusivo). "
        "Horas en Europe/Madrid.</p>"
        '<p class="a-prose">Cada métrica se calcula con los intervalos entre buses '
        "inferidos en cada ventana. El intervalo de confianza del cambio (después − antes) "
        f"es bootstrap percentil al {confidence} con {parameters.bootstrap_samples} "
        "remuestreos; «significativo» indica que el intervalo no contiene el cero. El "
        "p-valor de Mann-Whitney contrasta si la distribución de intervalos ha cambiado. "
        "La saturación se mide contra la mediana por hora de la ventana anterior "
        f"(× {parameters.ratio:g}, mínimo {parameters.min_headway_seconds // 60} min).</p>"
        '<div class="a-grid">'
        + _metric("Observaciones", sample_count)
        + _metric("Rutas tratadas", len(treated))
        + _metric("Rutas de control", len(control))
        + _metric("Rutas descartadas", len(skipped))
        + "</div></section>"
    )

    body += (
        '<section class="a-section"><h2 class="a-section__title">Diferencias en diferencias'
        "</h2>"
        '<p class="a-section__note">Cambio medio en las rutas tratadas menos cambio medio en '
        "las de control: descuenta lo que varió en toda la red durante el mismo periodo. "
        "Requiere al menos una ruta de cada tipo con datos suficientes.</p>"
        + table(
            ["Métrica", "Δ tratadas", "Δ control", "Efecto neto", f"IC {confidence}", "Signif."],
            [
                [
                    METRIC_LABELS[d.metric],
                    fmt_delta(d.metric, d.treated_delta),
                    fmt_delta(d.metric, d.control_delta),
                    fmt_delta(d.metric, d.estimate),
                    f"[{fmt_delta(d.metric, d.ci_low)}, {fmt_delta(d.metric, d.ci_high)}]",
                    "sí" if d.significant else "no",
                ]
                for d in did
            ],
            "Efecto neto del evento",
            empty="Sin rutas de control: se muestra sólo el antes/después de cada ruta.",
        )
        + "</section>"
    )

    body += (
        '<section class="a-section"><h2 class="a-section__title">Resumen por ruta</h2>'
        + table(
            [
                "Ruta",
                "Rol",
                "Intervalos antes/después",
                "Intervalo medio",
                "Espera media",
                "Saturados",
                "Episodios/día",
            ],
            [
                [
                    i.route.key,
                    ROLE_LABEL[i.role],
                    f"{i.before.headways} / {i.after.headways}",
                    *[
                        f"{fmt(m, i.change(m).before)} → {fmt(m, i.change(m).after)}"
                        + (" *" if i.change(m).significant else "")
                        for m in METRICS
                    ],
                ]
                for i in impacts
            ],
            "Antes → después por ruta (* cambio significativo)",
            empty="Ninguna ruta con datos suficientes en ambas ventanas.",
        )
        + "</section>"
    )

    for impact in impacts:
        hours = sorted(set(impact.before.hourly_mean) | set(impact.after.hourly_mean))
        body += (
            f'<section class="a-section"><h2 class="a-section__title">{escape(impact.route.key)}'
            f" · {ROLE_LABEL[impact.role]}</h2>"
            f'<p class="a-section__note">Antes: {impact.before.headways} intervalos en '
            f"{impact.before.days} días (CV {impact.before.cv:.2f}). Después: "
            f"{impact.after.headways} intervalos en {impact.after.days} días "
            f"(CV {impact.after.cv:.2f}).</p>"
            + table(
                ["Métrica", "Antes", "Después", "Cambio", f"IC {confidence}", "p", "Signif."],
                [_change_row(c) for c in impact.changes],
                "Cambio por métrica",
            )
            + _chart(
                "bar",
                " min",
                "Intervalo medio por hora: antes y después",
                table(
                    ["Hora", "Antes", "Después"],
                    [
                        [
                            f"{hour:02}:00",
                            _hour(impact.before.hourly_mean, hour),
                            _hour(impact.after.hourly_mean, hour),
                        ]
                        for hour in hours
                    ],
                    "Intervalo medio por hora local",
                ),
                "En pantallas estrechas, desplaza el gráfico horizontalmente o abre su tabla.",
            )
            + "</section>"
        )

    body += (
        '<section class="a-section"><h2 class="a-section__title">Rutas descartadas</h2>'
        + table(
            ["Línea", "Parada", "Destino", "Motivo"],
            [[s.line, s.stop_id, s.destination, s.reason] for s in skipped],
            "Rutas sin comparación",
            empty="Todas las rutas con pasos inferidos tienen comparación.",
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


def _hour(values: dict[int, float], hour: int) -> str:
    return f"{values[hour]:.1f}" if hour in values else "0"
