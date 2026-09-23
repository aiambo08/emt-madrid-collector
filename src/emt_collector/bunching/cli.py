from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError

from emt_collector.bunching.data import load_history
from emt_collector.bunching.demo import synthetic_history
from emt_collector.bunching.detector import build_series, detect
from emt_collector.bunching.domain import Example, Parameters
from emt_collector.bunching.features import examples
from emt_collector.bunching.model import ForecastModel, train_model
from emt_collector.bunching.report import predict, write_outputs
from emt_collector.config import Settings
from emt_collector.db.repository import make_engine


def timestamp(value: str) -> datetime:
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Usa una fecha ISO 8601 con zona horaria.") from exc
    if at.tzinfo is None:
        raise argparse.ArgumentTypeError("Incluye zona horaria: Z o +02:00, por ejemplo.")
    return at.astimezone(timezone.utc)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="emt-bunching", description="Detector y predictor de bunching"
    )
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser(
        "demo", help="datos sintéticos + entrenamiento + informe sin API ni BD"
    )
    demo.add_argument("--days", type=int, default=28)
    demo.add_argument("--seed", type=int, default=42)
    analyze = commands.add_parser("analyze", help="analizar histórico de la BD y entrenar")
    analyze.add_argument("--start", type=timestamp, required=True)
    analyze.add_argument("--end", type=timestamp, default=None)
    analyze.add_argument("--stop", action="append", default=[], help="parada a incluir; repetible")
    for command in (demo, analyze):
        command.add_argument(
            "--output", type=Path, required=True, help="directorio nuevo de resultados"
        )
        command.add_argument("--gap-minutes", type=int, default=20)
        command.add_argument("--cluster-seconds", type=int, default=180)
        command.add_argument("--min-buses", type=int, default=3)
        command.add_argument("--horizon-minutes", type=int, default=15)
        command.add_argument("--max-gap-seconds", type=int, default=90)
        command.add_argument("--near-seconds", type=int, default=60)
        command.add_argument("--near-metres", type=int, default=150)
        command.add_argument("--vanish-seconds", type=int, default=180)
    forecast = commands.add_parser(
        "predict", help="aplicar un modelo real a las muestras recientes"
    )
    forecast.add_argument("--model", type=Path, required=True)
    forecast.add_argument("--at", type=timestamp, default=None)
    return root


def run(args: argparse.Namespace) -> int:
    model: ForecastModel | None
    if args.command == "predict":
        model = ForecastModel.load(args.model)
        if model.source == "synthetic":
            raise ValueError("El modelo sintético solo sirve para la demo; entrena con analyze.")
        at = args.at or datetime.now(timezone.utc)
        start = at - timedelta(
            seconds=model.parameters.lookback_seconds
            + model.parameters.bus_cooldown_seconds
            + model.parameters.near_seconds
            + model.parameters.max_latency_seconds
        )
        engine = make_engine(Settings().resolved_database_url())
        try:
            observations, gaps = load_history(
                engine, start, at, stops=sorted({route[1] for route in model.routes})
            )
        finally:
            engine.dispose()
        predictions = predict(build_series(observations, gaps, model.parameters), model, at)
        print(json.dumps([p.model_dump(mode="json") for p in predictions], indent=2))
        return 0 if any(p.status == "ok" for p in predictions) else 3

    if args.output.exists():
        raise ValueError(
            "El directorio de salida ya existe; usa uno nuevo para conservar resultados."
        )
    parameters = Parameters(
        gap_seconds=args.gap_minutes * 60,
        cluster_seconds=args.cluster_seconds,
        min_buses=args.min_buses,
        horizon_seconds=args.horizon_minutes * 60,
        max_gap_seconds=args.max_gap_seconds,
        near_seconds=args.near_seconds,
        near_metres=args.near_metres,
        vanish_seconds=args.vanish_seconds,
    )
    source: Literal["synthetic", "database"]
    if args.command == "demo":
        source = "synthetic"
        observations, gaps = synthetic_history(args.days, args.seed)
        start = min(row.sample_ts for row in observations)
        end = max(row.available_at for row in observations) + timedelta(seconds=1)
    else:
        source = "database"
        start, end = args.start, args.end or datetime.now(timezone.utc)
        engine = make_engine(Settings().resolved_database_url())
        try:
            observations, gaps = load_history(engine, start, end, stops=args.stop)
        finally:
            engine.dispose()
    series = build_series(observations, gaps, parameters)
    events = []
    rows = []
    for item in series:
        found = detect(item)
        events.extend(found)
        rows.extend(examples(item, found, end))
    model = None
    holdout: list[tuple[Example, float, float]] = []
    training_error = None
    try:
        model, holdout = train_model(rows, parameters, source)
    except ValueError as exc:
        training_error = str(exc)
    predictions = predict(series, model, end) if model else []
    write_outputs(
        args.output,
        source,
        parameters,
        series,
        events,
        rows,
        model,
        holdout,
        predictions,
        training_error,
        len(observations),
        start,
        end,
    )
    print(
        json.dumps(
            {
                "source": source,
                "events": len(events),
                "labeled_windows": len(rows),
                "model": "trained" if model else "insufficient_data",
                "reason": training_error,
                "report": str(args.output / "report.html"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if model else 3


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
