from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from emt_collector.api.client import EMTError
from emt_collector.bunching.domain import Route
from emt_collector.bunching.features import MADRID
from emt_collector.collector import normalize_line
from emt_collector.impact.domain import METRIC_LABELS, METRICS
from emt_collector.impact.summary import ImpactSummary
from emt_collector.telegram.api import BotCommand
from emt_collector.telegram.data import Arrival, DataSource, Risk, Status

COMMANDS = [
    BotCommand(command="llegadas", description="Próximas llegadas: /llegadas <parada> [línea]"),
    BotCommand(command="riesgo", description="Riesgo previsto: /riesgo <parada> [línea]"),
    BotCommand(command="impacto", description="Antes/después del último evento: /impacto [parada]"),
    BotCommand(command="estado", description="Salud del recolector y modelos"),
    BotCommand(command="ayuda", description="Cómo usar el bot"),
]

HELP = (
    "<b>EMT Madrid · bot de servicio</b>\n"
    "/llegadas &lt;parada&gt; [línea] — próximas llegadas en tiempo real (API EMT)\n"
    "/riesgo &lt;parada&gt; [línea] — probabilidad de bunching y de intervalo saturado en los "
    "próximos minutos, según los modelos entrenados con el histórico\n"
    "/impacto [parada] — cambio del servicio antes y después del último evento analizado "
    "(emt-analysis run --event)\n"
    "/estado — último ciclo del recolector, gaps y modelos cargados\n"
    "/ayuda — este mensaje\n\n"
    "Las paradas son los códigos EMT (p. ej. 1182). El riesgo es una predicción sobre pasos "
    "inferidos, no una medida de ocupación."
)

STOP_RE = re.compile(r"^\d{1,6}$")
MAX_ARRIVALS = 12
MAX_IMPACT_ROUTES = 6


def command_parts(text: str) -> tuple[str, list[str]]:
    words = text.strip().split()
    if not words or not words[0].startswith("/"):
        return "", []
    name = words[0][1:].split("@", 1)[0].lower()
    return name, words[1:]


def local(at: datetime) -> str:
    return at.astimezone(MADRID).strftime("%H:%M")


def minutes(seconds: int) -> str:
    if seconds < 60:
        return "llegando"
    return f"{seconds // 60} min"


def percent(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)} %"


def format_arrivals(stop: str, name: str | None, line: str | None, rows: list[Arrival]) -> str:
    title = f"<b>Parada {escape(stop)}</b>"
    if name:
        title += f" · {escape(name)}"
    if line:
        title += f" · línea {escape(line)}"
    if not rows:
        return f"{title}\nSin llegadas previstas ahora mismo."
    lines = [title]
    for row in rows[:MAX_ARRIVALS]:
        eta = minutes(row.eta_seconds) if row.eta_seconds is not None else "sin estimación"
        distance = f" · {row.distance_m} m" if row.distance_m is not None else ""
        lines.append(f"<b>{escape(row.line)}</b> → {escape(row.destination)}: {eta}{distance}")
    if len(rows) > MAX_ARRIVALS:
        lines.append(f"… y {len(rows) - MAX_ARRIVALS} más")
    return "\n".join(lines)


def format_risks(stop: str, name: str | None, line: str | None, risks: list[Risk]) -> str:
    title = f"<b>Riesgo en parada {escape(stop)}</b>"
    if name:
        title += f" · {escape(name)}"
    if line:
        risks = [r for r in risks if r.route.line == line]
        title += f" · línea {escape(line)}"
    if not risks:
        return f"{title}\nNingún modelo cubre esta parada{' y línea' if line else ''}."
    lines = [title]
    for risk in risks:
        head = f"<b>{escape(risk.route.line)}</b> → {escape(risk.route.destination)}"
        if not risk.usable:
            lines.append(f"{head}: sin muestras recientes suficientes.")
            continue
        parts = []
        if risk.bunching_status == "ok":
            parts.append(
                f"bunching {percent(risk.bunching_probability)} en {risk.horizon_minutes} min"
            )
        if risk.saturation_status == "ok":
            wait = (
                f", espera prevista {risk.expected_wait_minutes:.0f} min"
                if risk.expected_wait_minutes is not None
                else ""
            )
            since = (
                f" (último bus hace {risk.minutes_since_last_bus:.0f} min"
                f", umbral {risk.threshold_minutes:.0f} min)"
                if risk.minutes_since_last_bus is not None and risk.threshold_minutes is not None
                else ""
            )
            parts.append(f"intervalo saturado {percent(risk.saturation_probability)}{wait}{since}")
        lines.append(f"{head}: " + "; ".join(parts))
    return "\n".join(lines)


def format_status(status: Status, expected_interval_seconds: int) -> str:
    lines = ["<b>Estado del recolector</b>"]
    cycle = status.last_cycle
    if cycle is None:
        lines.append("Sin ciclos registrados en la base de datos.")
    else:
        age = (status.now - cycle.started_at).total_seconds()
        stale = age > max(3 * expected_interval_seconds, 300)
        flag = "⚠️ " if stale or cycle.status != "ok" else ""
        lines.append(
            f"{flag}Último ciclo {local(cycle.started_at)} ({age / 60:.0f} min): "
            f"{escape(cycle.status)}, {cycle.stops_ok} paradas OK, {cycle.stops_failed} fallidas, "
            f"{cycle.arrivals_inserted} llegadas."
        )
    expected = 3600 / expected_interval_seconds
    lines.append(
        f"Última hora: {status.cycles_last_hour} ciclos de {expected:.0f} esperados, "
        f"{status.arrivals_last_hour} llegadas, {status.gaps_last_hour} gaps."
    )
    models = status.models
    if not models.bunching and not models.saturation:
        lines.append(
            "Modelos: ninguno cargado (ejecuta emt-analysis run con ≥7 días de histórico)."
        )
    else:
        generated = f" ({local(models.generated_at)})" if models.generated_at else ""
        lines.append(
            f"Modelos{generated}: bunching "
            f"{len(models.bunching.routes) if models.bunching else 0} rutas, saturación "
            f"{len(models.saturation.routes) if models.saturation else 0} rutas."
        )
    if models.impact:
        lines.append(
            f"Impacto: evento {local(models.impact.event)} "
            f"{models.impact.event.astimezone(MADRID):%d/%m}, "
            f"{len(models.impact.treated)} rutas tratadas (/impacto)."
        )
    return "\n".join(lines)


def _value(metric: str, value: float) -> str:
    return f"{value:.0%}" if metric == "saturation_rate" else f"{value:.1f}"


def _delta(metric: str, value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.0%}" if metric == "saturation_rate" else f"{sign}{value:.1f}"


def format_impact(summary: ImpactSummary | None, stop: str | None) -> str:
    if summary is None:
        return (
            "Sin análisis de impacto cargado: ejecuta emt-analysis run --event &lt;fecha&gt; "
            "(o emt-impact analyze) sobre el histórico."
        )
    day = summary.event.astimezone(MADRID).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"<b>Impacto del evento {day}</b>",
        f"Antes: {summary.start.astimezone(MADRID):%d/%m} → {day[:10]}; "
        f"después: hasta {summary.end_exclusive.astimezone(MADRID):%d/%m %H:%M}.",
    ]
    routes = summary.treated
    if stop is not None:
        routes = [r for r in routes if r.stop_id == stop]
        if not routes:
            return f"La parada {escape(stop)} no está entre las rutas tratadas del análisis."
    if summary.difference_in_differences and stop is None:
        lines.append("<b>Efecto neto</b> (tratadas − control):")
        for d in summary.difference_in_differences:
            mark = " *" if d.significant else ""
            lines.append(
                f"• {METRIC_LABELS[d.metric]}: {_delta(d.metric, d.estimate)} "
                f"[{_delta(d.metric, d.ci_low)}, {_delta(d.metric, d.ci_high)}]{mark}"
            )
    for route in routes[:MAX_IMPACT_ROUTES]:
        lines.append(
            f"<b>L{escape(route.line)} · {escape(route.stop_id)} → {escape(route.destination)}</b>"
        )
        for metric in METRICS:
            change = route.change(metric)
            if change is None:
                continue
            mark = " *" if change.significant else ""
            lines.append(
                f"• {METRIC_LABELS[metric]}: {_value(metric, change.before)} → "
                f"{_value(metric, change.after)} ({_delta(metric, change.delta)}){mark}"
            )
    if len(routes) > MAX_IMPACT_ROUTES:
        lines.append(
            f"… y {len(routes) - MAX_IMPACT_ROUTES} rutas más; filtra con /impacto &lt;parada&gt;."
        )
    if not routes:
        lines.append("Ninguna ruta tratada con datos suficientes en ambas ventanas.")
    lines.append("* cambio significativo (el intervalo de confianza no incluye el cero).")
    return "\n".join(lines)


def usage(command: str) -> str:
    return f"Uso: /{command} &lt;parada&gt; [línea], p. ej. /{command} 1182 45"


def parse_target(command: str, args: list[str]) -> tuple[str, str | None] | str:
    if not args or not STOP_RE.match(args[0]):
        return usage(command)
    line = normalize_line(args[1]) if len(args) > 1 else None
    return args[0], line


class Handlers:
    def __init__(self, source: DataSource, expected_interval_seconds: int) -> None:
        self._source = source
        self._interval = expected_interval_seconds

    def handle(self, text: str, now: datetime) -> str | None:
        command, args = command_parts(text)
        if command in {"start", "ayuda", "help"}:
            return HELP
        if command == "llegadas":
            return self.arrivals(args)
        if command == "riesgo":
            return self.risk(args, now)
        if command == "estado":
            return format_status(self._source.status(now), self._interval)
        if command == "impacto":
            if args and not STOP_RE.match(args[0]):
                return "Uso: /impacto [parada], p. ej. /impacto 1182"
            return format_impact(self._source.models().impact, args[0] if args else None)
        if command:
            return f"Comando desconocido: /{escape(command)}. Usa /ayuda."
        return None

    def arrivals(self, args: list[str]) -> str:
        target = parse_target("llegadas", args)
        if isinstance(target, str):
            return target
        stop, line = target
        try:
            rows = self._source.arrivals(stop, line)
        except EMTError as exc:
            return (
                f"La API de la EMT no respondió para la parada {escape(stop)}: {escape(str(exc))}"
            )
        return format_arrivals(stop, self._source.stop_name(stop), line, rows)

    def risk(self, args: list[str], now: datetime) -> str:
        target = parse_target("riesgo", args)
        if isinstance(target, str):
            return target
        stop, line = target
        risks = self._source.risks([stop], now)
        return format_risks(stop, self._source.stop_name(stop), line, risks)


@dataclass
class Alerter:
    """Avisa cuando un modelo supera `probability` en una ruta, con enfriamiento por ruta."""

    probability: float
    cooldown: timedelta
    _last: dict[tuple[str, str, str, str], datetime] = field(default_factory=dict)

    def messages(self, risks: list[Risk], now: datetime, names: dict[str, str | None]) -> list[str]:
        out = []
        for risk in risks:
            route = risk.route
            where = f"parada {escape(route.stop_id)}"
            if names.get(route.stop_id):
                where += f" ({escape(names[route.stop_id] or '')})"
            head = f"<b>{escape(route.line)}</b> → {escape(route.destination)} · {where}"
            if (
                risk.bunching_status == "ok"
                and risk.bunching_probability is not None
                and risk.bunching_probability >= self.probability
                and self._fire(("bunching", *_key(route)), now)
            ):
                out.append(
                    f"⚠️ Riesgo de bunching {percent(risk.bunching_probability)} en los próximos "
                    f"{risk.horizon_minutes} min\n{head}"
                )
            if (
                risk.saturation_status == "ok"
                and risk.saturation_probability is not None
                and risk.saturation_probability >= self.probability
                and self._fire(("saturation", *_key(route)), now)
            ):
                wait = (
                    f", espera prevista {risk.expected_wait_minutes:.0f} min"
                    if risk.expected_wait_minutes is not None
                    else ""
                )
                out.append(
                    f"⚠️ Intervalo saturado probable {percent(risk.saturation_probability)}"
                    f"{wait}\n{head}"
                )
        return out

    def _fire(self, key: tuple[str, str, str, str], now: datetime) -> bool:
        last = self._last.get(key)
        if last is not None and now - last < self.cooldown:
            return False
        self._last[key] = now
        return True


def _key(route: Route) -> tuple[str, str, str]:
    return route.line, route.stop_id, route.destination
