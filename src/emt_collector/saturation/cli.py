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
from emt_collector.config import Settings
from emt_collector.db.repository import make_engine
from emt_collector.saturation.demo import synthetic_history
from emt_collector.saturation.domain import SaturationParameters
from emt_collector.saturation.headways import Threshold, build_reference, headways, windows
from emt_collector.saturation.model import SaturationModel, Scored, train_model
from emt_collector.saturation.report import predict, write_outputs


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="emt-saturation",
        description="Saturación del servicio: intervalos anómalos y espera prevista",
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
        command.add_argument("--ratio", type=float, default=1.5)
        command.add_argument("--min-headway-minutes", type=float, default=12)
        command.add_argument("--max-wait-minutes", type=int, default=90)
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
    model: SaturationModel | None
    if args.command == "predict":
        model = SaturationModel.load(args.model)
        if model.source == "synthetic":
            raise ValueError("El modelo sintético solo sirve para la demo; entrena con analyze.")
        at = args.at or datetime.now(timezone.utc)
        p = model.parameters
        start = at - timedelta(
            seconds=p.lookback_seconds
            + p.bus_cooldown_seconds
            + p.near_seconds
            + p.max_latency_seconds
        )
        engine = make_engine(Settings().resolved_database_url())
        try:
            observations, gaps = load_history(
                engine, start, at, stops=sorted({route[1] for route in model.routes})
            )
        finally:
            engine.dispose()
        predictions = predict(build_series(observations, gaps, p), model, at)
        print(json.dumps([p.model_dump(mode="json") for p in predictions], indent=2))
        return 0 if any(p.status == "ok" for p in predictions) else 3

    if args.output.exists():
        raise ValueError(
            "El directorio de salida ya existe; usa uno nuevo para conservar resultados."
        )
    parameters = SaturationParameters(
        ratio=args.ratio,
        min_headway_seconds=round(args.min_headway_minutes * 60),
        max_wait_seconds=args.max_wait_minutes * 60,
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
    intervals = []
    rows = []
    for item in series:
        found = headways(item)
        intervals.extend(found)
        rows.extend(windows(item, found, end))
    model = None
    scored: list[Scored] = []
    training_error = None
    try:
        model, scored = train_model(rows, intervals, parameters, source)
    except ValueError as exc:
        training_error = str(exc)
    if model:
        reference = model.reference
        reference_scope = "el tramo de entrenamiento"
    else:
        reference = build_reference(intervals)
        reference_scope = "todo el periodo (sin modelo)"
    saturated = Threshold(reference, parameters).saturated(intervals)
    predictions = predict(series, model, end) if model else []
    write_outputs(
        args.output,
        source,
        parameters,
        series,
        intervals,
        saturated,
        reference,
        reference_scope,
        rows,
        model,
        scored,
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
                "headways": len(intervals),
                "saturated_headways": len(saturated),
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
