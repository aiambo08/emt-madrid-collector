from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError

from emt_collector.bunching.cli import timestamp
from emt_collector.bunching.data import load_history
from emt_collector.bunching.detector import build_series
from emt_collector.config import Settings
from emt_collector.db.repository import make_engine
from emt_collector.frequency.demo import synthetic_history
from emt_collector.frequency.domain import FrequencyParameters, RoutePlan, Skipped
from emt_collector.frequency.optimizer import plan_route
from emt_collector.frequency.report import write_outputs
from emt_collector.frequency.service import Demand, hourly_service, read_demand_csv
from emt_collector.saturation.headways import Threshold, build_reference, headways

DEMO_DEMAND = {
    6: 2,
    7: 6,
    8: 10,
    9: 8,
    10: 4,
    11: 4,
    12: 4,
    13: 5,
    14: 5,
    15: 4,
    16: 5,
    17: 7,
    18: 9,
    19: 8,
    20: 5,
    21: 3,
    22: 2,
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="emt-frequency",
        description="Optimización de frecuencias: reparto de horas-bus por franja horaria",
    )
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="datos sintéticos + plan + informe sin API ni BD")
    demo.add_argument("--days", type=int, default=14)
    demo.add_argument("--seed", type=int, default=7)
    analyze = commands.add_parser("analyze", help="planificar a partir del histórico de la BD")
    analyze.add_argument("--start", type=timestamp, required=True)
    analyze.add_argument("--end", type=timestamp, default=None)
    analyze.add_argument("--stop", action="append", default=[], help="parada a incluir; repetible")
    analyze.add_argument("--demand", choices=["proxy", "uniform"], default="proxy")
    analyze.add_argument(
        "--demand-csv", type=Path, default=None, help="perfil horario: columnas hour,weight[,line]"
    )
    for command in (demo, analyze):
        command.add_argument(
            "--output", type=Path, required=True, help="directorio nuevo de resultados"
        )
        command.add_argument("--service-start-hour", type=int, default=6)
        command.add_argument("--service-end-hour", type=int, default=23)
        command.add_argument("--min-planned-headway-minutes", type=float, default=3)
        command.add_argument("--max-planned-headway-minutes", type=float, default=20)
        command.add_argument("--min-hour-samples", type=int, default=5)
        command.add_argument("--min-days", type=int, default=3)
        command.add_argument("--target-cv", type=float, default=0.3)
        command.add_argument("--ratio", type=float, default=1.5)
        command.add_argument("--min-headway-minutes", type=float, default=12)
        command.add_argument("--max-gap-seconds", type=int, default=90)
        command.add_argument("--near-seconds", type=int, default=60)
        command.add_argument("--near-metres", type=int, default=150)
    return root


def run(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(
            "El directorio de salida ya existe; usa uno nuevo para conservar resultados."
        )
    if args.service_end_hour <= args.service_start_hour:
        raise ValueError("La hora de fin del servicio debe ser posterior a la de inicio.")
    if args.max_planned_headway_minutes <= args.min_planned_headway_minutes:
        raise ValueError("El intervalo planificado máximo debe superar al mínimo.")
    demand: Demand | None = None
    source: Literal["synthetic", "database"]
    if args.command == "demo":
        source = "synthetic"
        mode: Literal["proxy", "uniform", "csv"] = "csv"
        demand = {(None, hour): float(weight) for hour, weight in DEMO_DEMAND.items()}
        observations, gaps = synthetic_history(args.days, args.seed)
        start = min(row.sample_ts for row in observations)
        end = max(row.available_at for row in observations) + timedelta(seconds=1)
    else:
        source = "database"
        if args.demand_csv:
            mode = "csv"
            demand = read_demand_csv(args.demand_csv)
        else:
            mode = args.demand
        start, end = args.start, args.end or datetime.now(timezone.utc)
        engine = make_engine(Settings().resolved_database_url())
        try:
            observations, gaps = load_history(engine, start, end, stops=args.stop)
        finally:
            engine.dispose()
    parameters = FrequencyParameters(
        service_start_hour=args.service_start_hour,
        service_end_hour=args.service_end_hour,
        min_planned_headway_minutes=args.min_planned_headway_minutes,
        max_planned_headway_minutes=args.max_planned_headway_minutes,
        min_hour_samples=args.min_hour_samples,
        min_days=args.min_days,
        target_cv=args.target_cv,
        demand_mode=mode,
        ratio=args.ratio,
        min_headway_seconds=round(args.min_headway_minutes * 60),
        max_gap_seconds=args.max_gap_seconds,
        near_seconds=args.near_seconds,
        near_metres=args.near_metres,
    )
    series = build_series(observations, gaps, parameters)
    intervals = {item.route: headways(item) for item in series}
    threshold = Threshold(
        build_reference([row for rows in intervals.values() for row in rows]), parameters
    )
    plans: list[RoutePlan] = []
    skipped: list[Skipped] = []
    for item in series:
        service = hourly_service(item, intervals[item.route], threshold, parameters, demand)
        result = plan_route(item, service, parameters)
        if isinstance(result, RoutePlan):
            plans.append(result)
        else:
            skipped.append(result)
    write_outputs(args.output, source, parameters, plans, skipped, len(observations), start, end)
    if demand is not None:
        with (args.output / "demand.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["line", "hour", "weight"])
            for (line, hour), weight in sorted(
                demand.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])
            ):
                writer.writerow([line or "", hour, weight])
    print(
        json.dumps(
            {
                "source": source,
                "demand": mode,
                "routes_planned": len(plans),
                "routes_skipped": len(skipped),
                "report": str(args.output / "report.html"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if plans else 3


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
