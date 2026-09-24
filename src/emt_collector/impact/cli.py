from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError

from emt_collector.bunching.cli import timestamp
from emt_collector.bunching.data import load_history
from emt_collector.bunching.detector import build_series
from emt_collector.bunching.domain import Route
from emt_collector.collector import normalize_line
from emt_collector.config import Settings
from emt_collector.db.repository import make_engine
from emt_collector.impact.analysis import analyze, clip_windows
from emt_collector.impact.demo import CONTROL_LINE, synthetic_history
from emt_collector.impact.domain import ImpactParameters
from emt_collector.impact.report import write_outputs


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="emt-impact",
        description="Análisis de impacto: servicio antes y después de un evento",
    )
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="datos sintéticos + comparación + informe")
    demo.add_argument("--days", type=int, default=14)
    demo.add_argument("--seed", type=int, default=11)
    analyze = commands.add_parser("analyze", help="comparar ventanas del histórico de la BD")
    analyze.add_argument(
        "--event", type=timestamp, required=True, help="instante del evento (ISO 8601 con zona)"
    )
    analyze.add_argument("--before-days", type=int, default=7)
    analyze.add_argument("--after-days", type=int, default=None, help="por defecto --before-days")
    analyze.add_argument(
        "--stop", action="append", default=[], help="parada afectada por el evento; repetible"
    )
    analyze.add_argument(
        "--control-stop",
        action="append",
        default=[],
        help="parada no afectada que sirve de control; repetible",
    )
    analyze.add_argument(
        "--control-line",
        action="append",
        default=[],
        help="línea no afectada que sirve de control; repetible",
    )
    for command in (demo, analyze):
        command.add_argument(
            "--output", type=Path, required=True, help="directorio nuevo de resultados"
        )
        command.add_argument("--min-headways", type=int, default=20)
        command.add_argument("--min-days", type=int, default=2)
        command.add_argument("--bootstrap-samples", type=int, default=1000)
        command.add_argument("--confidence", type=float, default=0.95)
        command.add_argument("--ratio", type=float, default=1.5)
        command.add_argument("--min-headway-minutes", type=float, default=12)
        command.add_argument("--max-gap-seconds", type=int, default=90)
        command.add_argument("--near-seconds", type=int, default=60)
        command.add_argument("--near-metres", type=int, default=150)
        command.add_argument("--vanish-seconds", type=int, default=180)
    return root


def run(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(
            "El directorio de salida ya existe; usa uno nuevo para conservar resultados."
        )
    source: Literal["synthetic", "database"]
    control_stops: set[str]
    control_lines: set[str]
    if args.command == "demo":
        source = "synthetic"
        observations, gaps, event = synthetic_history(args.days, args.seed)
        start = min(row.sample_ts for row in observations)
        end = max(row.available_at for row in observations) + timedelta(seconds=1)
        control_stops, control_lines = set(), {CONTROL_LINE}
    else:
        source = "database"
        event = args.event
        start, end = clip_windows(
            event, args.before_days, args.after_days or args.before_days, datetime.now(timezone.utc)
        )
        control_stops = set(args.control_stop)
        control_lines = {normalize_line(line) for line in args.control_line}
        stops = [*args.stop, *args.control_stop] if args.stop else []
        engine = make_engine(Settings().resolved_database_url())
        try:
            observations, gaps = load_history(engine, start, end, stops=stops)
        finally:
            engine.dispose()
    parameters = ImpactParameters(
        min_headways=args.min_headways,
        min_days=args.min_days,
        bootstrap_samples=args.bootstrap_samples,
        confidence=args.confidence,
        ratio=args.ratio,
        min_headway_seconds=round(args.min_headway_minutes * 60),
        max_gap_seconds=args.max_gap_seconds,
        near_seconds=args.near_seconds,
        near_metres=args.near_metres,
        vanish_seconds=args.vanish_seconds,
    )

    def role_of(route: Route) -> Literal["treated", "control"]:
        if route.stop_id in control_stops or route.line in control_lines:
            return "control"
        return "treated"

    series = build_series(observations, gaps, parameters)
    impacts, did, skipped = analyze(series, event, start, end, parameters, role_of)
    write_outputs(
        args.output, source, parameters, event, start, end, impacts, did, skipped, len(observations)
    )
    treated = [i for i in impacts if i.role == "treated"]
    print(
        json.dumps(
            {
                "source": source,
                "event": event.isoformat(),
                "routes_treated": len(treated),
                "routes_control": len(impacts) - len(treated),
                "routes_skipped": len(skipped),
                "significant_changes": sum(1 for i in treated for c in i.changes if c.significant),
                "difference_in_differences": bool(did),
                "report": str(args.output / "report.html"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if treated else 3


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except SQLAlchemyError:
        print(
            "No se pudo consultar la BD. Revisa conexión, tablas y permisos de lectura.",
            file=sys.stderr,
        )
        return 2
    except (ValueError, OSError) as exc:
        print(f"Error de análisis: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
