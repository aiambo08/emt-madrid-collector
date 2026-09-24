from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType

import structlog

from emt_collector.bunching import cli as bunching
from emt_collector.bunching.cli import timestamp
from emt_collector.frequency import cli as frequency
from emt_collector.impact import cli as impact
from emt_collector.logging_setup import configure_logging
from emt_collector.saturation import cli as saturation

log = structlog.get_logger(__name__)

ANALYSES: dict[str, Callable[[list[str] | None], int]] = {
    "bunching": bunching.main,
    "saturation": saturation.main,
    "frequency": frequency.main,
}
STATUS = {0: "ok", 3: "insufficient_data"}


@dataclass(frozen=True)
class Outcome:
    analysis: str
    exit_code: int
    status: str
    output: str


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emt-analysis",
        description=(
            "Ejecuta los analyze de bunching, saturación, frecuencias y, con --event, impacto "
            "sobre el histórico."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="una ejecución (o periódica con --every-hours)")
    run.add_argument("--output", type=Path, default=Path("reports"))
    run.add_argument("--days", type=int, default=14, help="ventana de histórico (por defecto 14)")
    run.add_argument("--stop", action="append", default=[], help="parada a incluir; repetible")
    run.add_argument(
        "--only",
        action="append",
        choices=sorted([*ANALYSES, "impact"]),
        default=[],
        help="repetible",
    )
    run.add_argument(
        "--event",
        type=timestamp,
        default=None,
        help="añade el análisis de impacto antes/después de este instante (ISO 8601 con zona)",
    )
    run.add_argument(
        "--control-stop", action="append", default=[], help="parada de control del impacto"
    )
    run.add_argument(
        "--control-line", action="append", default=[], help="línea de control del impacto"
    )
    run.add_argument(
        "--every-hours",
        type=float,
        default=0,
        help="repetir cada N horas hasta SIGTERM (0 = ejecutar una vez y salir)",
    )
    return parser


def run_once(
    output: Path,
    days: int,
    stops: list[str],
    only: list[str],
    now: datetime,
    event: datetime | None = None,
    control_stops: list[str] | None = None,
    control_lines: list[str] | None = None,
) -> list[Outcome]:
    start = now - timedelta(days=days)
    folder = output / now.strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True)
    common = ["--start", start.isoformat(), "--end", now.isoformat()]
    for stop in stops:
        common += ["--stop", stop]
    outcomes = []
    for name, main in ANALYSES.items():
        if only and name not in only:
            continue
        target = folder / name
        code = main(["analyze", "--output", str(target), *common])
        outcomes.append(Outcome(name, code, STATUS.get(code, "error"), str(target)))
        log.info("analysis.done", analysis=name, exit_code=code, output=str(target))
    if event is not None and (not only or "impact" in only):
        target = folder / "impact"
        argv = ["analyze", "--output", str(target), "--event", event.isoformat()]
        if start < event < now:
            argv += [
                "--before-days",
                str(max(1, (event - start).days)),
                "--after-days",
                str(max(1, math.ceil((now - event).total_seconds() / 86400))),
            ]
        for stop in stops:
            argv += ["--stop", stop]
        for stop in control_stops or []:
            argv += ["--control-stop", stop]
        for line in control_lines or []:
            argv += ["--control-line", line]
        code = impact.main(argv)
        outcomes.append(Outcome("impact", code, STATUS.get(code, "error"), str(target)))
        log.info("analysis.done", analysis="impact", exit_code=code, output=str(target))
    summary = json.dumps(
        {
            "generated_at": now.isoformat(),
            "start": start.isoformat(),
            "end": now.isoformat(),
            "event": event.isoformat() if event else None,
            "stops": stops,
            "folder": str(folder),
            "results": [asdict(o) for o in outcomes],
        },
        ensure_ascii=False,
        indent=2,
    )
    (folder / "summary.json").write_text(summary, encoding="utf-8")
    (output / "latest.json").write_text(summary, encoding="utf-8")
    return outcomes


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    configure_logging("INFO", "json")
    stopping = False

    def _stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        log.info("analysis.stopping", signal=signal.Signals(signum).name)

    if args.every_hours:
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    worst = 0
    while True:
        try:
            outcomes = run_once(
                args.output,
                args.days,
                args.stop,
                args.only,
                datetime.now(timezone.utc),
                args.event,
                args.control_stop,
                args.control_line,
            )
            worst = max(o.exit_code for o in outcomes) if outcomes else 0
        except OSError as exc:
            log.error("analysis.failed", error=str(exc))
            worst = 2
        if not args.every_hours:
            return worst
        deadline = time.monotonic() + args.every_hours * 3600
        while not stopping and time.monotonic() < deadline:
            time.sleep(1)
        if stopping:
            return 0


if __name__ == "__main__":
    sys.exit(main())
