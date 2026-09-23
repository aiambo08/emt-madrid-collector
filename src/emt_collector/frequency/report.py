from __future__ import annotations

import csv
import json
from datetime import datetime
from html import escape
from importlib.resources import files
from pathlib import Path
from typing import Literal

from emt_collector.bunching.report import local, table
from emt_collector.frequency.domain import (
    FrequencyParameters,
    HourlyService,
    RoutePlan,
    Skipped,
    expected_wait,
)
from emt_collector.saturation.report import _chart, _metric

TITLE = "Optimización de frecuencias · EMT Madrid"
SUBTITLE = (
    "Intervalo, regularidad y espera observados por línea, parada y hora; "
    "reparto propuesto de las mismas horas-bus para reducir la espera ponderada."
)
FOOTER = (
    "Sin datos de pasajeros: la demanda es un peso por hora (proxy, uniforme o CSV propio). "
    "Los buses en servicio se infieren del tiempo que tarda cada bus en volver a la parada."
)
DEMAND_LABEL = {
    "proxy": "proxy interno (buses observados/día × (1 + tasa de saturación))",
    "uniform": "uniforme (misma importancia para todas las horas)",
    "csv": "perfil horario aportado por CSV",
}


def write_outputs(
    output: Path,
    source: Literal["synthetic", "database"],
    parameters: FrequencyParameters,
    plans: list[RoutePlan],
    skipped: list[Skipped],
    sample_count: int,
    start: datetime,
    end: datetime,
) -> None:
    output.mkdir(parents=True, exist_ok=False)
    with (output / "plan.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "line",
                "stop_id",
                "destination",
                "hour",
                "headways",
                "days",
                "weight",
                "mean_headway_minutes",
                "cv",
                "saturated",
                "current_wait_minutes",
                "current_buses",
                "proposed_buses",
                "proposed_headway_minutes",
                "proposed_wait_minutes",
            ]
        )
        for plan in plans:
            for h in plan.hours:
                s = h.service
                writer.writerow(
                    [
                        plan.route.line,
                        plan.route.stop_id,
                        plan.route.destination,
                        s.hour,
                        s.headways,
                        s.days,
                        round(s.weight, 3),
                        round(s.mean_headway_minutes, 2),
                        round(s.cv, 3),
                        s.saturated,
                        round(h.current_wait_minutes, 2),
                        round(h.current_buses, 2),
                        h.proposed_buses,
                        round(h.proposed_headway_minutes, 2),
                        round(h.proposed_wait_minutes, 2),
                    ]
                )
    summary = {
        "source": source,
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "parameters": parameters.model_dump(),
        "observations": sample_count,
        "routes": [
            {
                "line": plan.route.line,
                "stop_id": plan.route.stop_id,
                "destination": plan.route.destination,
                "cycle_minutes": round(plan.cycle_minutes, 1),
                "cycle_samples": plan.cycle_samples,
                "bus_hours": round(plan.bus_hours, 2),
                "hours": len(plan.hours),
                "current_wait_minutes": round(_avg(plan, "current"), 2),
                "proposed_wait_minutes": round(_avg(plan, "proposed"), 2),
                "regular_wait_minutes": round(_avg(plan, "regular"), 2),
            }
            for plan in plans
        ],
        "skipped": [item.model_dump() for item in skipped],
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.html").write_text(
        _html(source, parameters, plans, skipped, sample_count, start, end), encoding="utf-8"
    )


def _regular_wait(service: HourlyService, target_cv: float) -> float:
    return expected_wait(service.mean_headway_minutes, min(service.cv, target_cv))


def _avg(plan: RoutePlan, kind: str) -> float:
    total = {
        "current": plan.current_weighted_wait,
        "proposed": plan.proposed_weighted_wait,
        "regular": plan.regular_weighted_wait,
    }[kind]
    return total / plan.total_weight


def _html(
    source: Literal["synthetic", "database"],
    parameters: FrequencyParameters,
    plans: list[RoutePlan],
    skipped: list[Skipped],
    sample_count: int,
    start: datetime,
    end: datetime,
) -> str:
    label = (
        "DEMO SINTÉTICA · no son datos reales de EMT"
        if source == "synthetic"
        else "HISTÓRICO DE LA BASE DE DATOS"
    )
    hours_total = sum(len(plan.hours) for plan in plans)
    body = (
        "<style>@media screen and (max-width:640px){"
        ".a-panel[data-a-chart]{overflow-x:auto}"
        ".a-panel[data-a-chart] .a-chart{min-width:640px}"
        ".a-chart,.a-chart__band{touch-action:pan-x pan-y}"
        "}</style>"
        f'<section class="a-section"><div class="a-callout a-callout--risk">{label}</div>'
        f'<p class="a-prose">Periodo: {local(start)} → {local(end)} (fin exclusivo). '
        f"Horas en Europe/Madrid, servicio {parameters.service_start_hour:02}:00–"
        f"{parameters.service_end_hour:02}:00.</p>"
        '<p class="a-prose">Espera media de un pasajero que llega al azar: '
        "H̄ · (1 + CV²) / 2, con H̄ el intervalo medio observado y CV su coeficiente de "
        "variación. Buses en servicio: tiempo de ciclo / intervalo. La propuesta reparte las "
        "mismas horas-bus entre franjas minimizando la espera ponderada por demanda; el "
        f"intervalo propuesto se acota a {parameters.min_planned_headway_minutes:g}–"
        f"{parameters.max_planned_headway_minutes:g} min y la regularidad de cada hora se "
        "mantiene.</p>"
        f'<p class="a-prose">Peso de demanda: {escape(DEMAND_LABEL[parameters.demand_mode])}.</p>'
        '<div class="a-grid">'
        + _metric("Observaciones", sample_count)
        + _metric("Rutas planificadas", len(plans))
        + _metric("Franjas horarias", hours_total)
        + _metric("Rutas descartadas", len(skipped))
        + "</div></section>"
    )

    body += (
        '<section class="a-section"><h2 class="a-section__title">Resumen por ruta</h2>'
        '<p class="a-section__note">Espera media ponderada por demanda. «Regular» es la espera '
        "con los intervalos actuales si fueran perfectamente regulares (CV = 0): mide cuánto "
        "cuesta el bunching frente a cuánto se gana moviendo buses.</p>"
        + table(
            [
                "Ruta",
                "Ciclo (min)",
                "Retornos",
                "Horas-bus",
                "Espera actual",
                "Espera propuesta",
                "Espera regular",
                "Mejora",
            ],
            [
                [
                    plan.route.key,
                    f"{plan.cycle_minutes:.0f}",
                    str(plan.cycle_samples),
                    f"{plan.bus_hours:.1f}",
                    f"{_avg(plan, 'current'):.2f} min",
                    f"{_avg(plan, 'proposed'):.2f} min",
                    f"{_avg(plan, 'regular'):.2f} min",
                    f"{1 - _avg(plan, 'proposed') / _avg(plan, 'current'):.1%}",
                ]
                for plan in plans
            ],
            "Comparación actual, propuesta y regular",
            empty="Ninguna ruta con datos suficientes para planificar.",
        )
        + "</section>"
    )

    for plan in plans:
        hours = plan.hours
        body += (
            f'<section class="a-section"><h2 class="a-section__title">{escape(plan.route.key)}'
            "</h2>"
            f'<p class="a-section__note">Ciclo estimado {plan.cycle_minutes:.0f} min '
            f"({plan.cycle_samples} retornos del mismo bus); {plan.bus_hours:.1f} horas-bus "
            "observadas por día repartidas entre las franjas con datos.</p>"
            + _chart(
                "bar",
                " buses",
                "Buses en servicio: actual y propuesto",
                table(
                    ["Hora", "Actual", "Propuesto"],
                    [
                        [f"{h.service.hour:02}:00", f"{h.current_buses:.1f}", str(h.proposed_buses)]
                        for h in hours
                    ],
                    "Buses en servicio por hora",
                ),
                "En pantallas estrechas, desplaza el gráfico horizontalmente o abre su tabla.",
            )
            + _chart(
                "bar",
                " min",
                "Espera media: actual, propuesta y con regularidad objetivo",
                table(
                    ["Hora", "Actual", "Propuesta", f"Actual con CV ≤ {parameters.target_cv:g}"],
                    [
                        [
                            f"{h.service.hour:02}:00",
                            f"{h.current_wait_minutes:.1f}",
                            f"{h.proposed_wait_minutes:.1f}",
                            f"{_regular_wait(h.service, parameters.target_cv):.1f}",
                        ]
                        for h in hours
                    ],
                    "Minutos de espera media por hora",
                ),
            )
            + _chart(
                "bar",
                "",
                "Peso de demanda por hora",
                table(
                    ["Hora", "Peso"],
                    [[f"{h.service.hour:02}:00", f"{h.service.weight:.2f}"] for h in hours],
                    "Peso relativo usado en la optimización",
                ),
            )
            + table(
                [
                    "Hora",
                    "Intervalos",
                    "Días",
                    "Intervalo medio",
                    "CV",
                    "Saturados",
                    "Buses actual",
                    "Buses propuesto",
                    "Intervalo propuesto",
                    "Espera actual",
                    "Espera propuesta",
                ],
                [
                    [
                        f"{h.service.hour:02}:00",
                        str(h.service.headways),
                        str(h.service.days),
                        f"{h.service.mean_headway_minutes:.1f} min",
                        f"{h.service.cv:.2f}",
                        f"{h.service.saturation_rate:.0%}",
                        f"{h.current_buses:.1f}",
                        str(h.proposed_buses),
                        f"{h.proposed_headway_minutes:.1f} min",
                        f"{h.current_wait_minutes:.1f} min",
                        f"{h.proposed_wait_minutes:.1f} min",
                    ]
                    for h in hours
                ],
                "Detalle por hora",
            )
            + "</section>"
        )

    body += (
        '<section class="a-section"><h2 class="a-section__title">Rutas descartadas</h2>'
        + table(
            ["Línea", "Parada", "Destino", "Motivo"],
            [[s.line, s.stop_id, s.destination, s.reason] for s in skipped],
            "Rutas sin plan",
            empty="Todas las rutas con pasos inferidos tienen plan.",
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
