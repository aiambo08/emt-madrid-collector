from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeVar

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from emt_collector.bunching.model import ForecastModel
from emt_collector.saturation.model import SaturationModel

log = structlog.get_logger(__name__)

M = TypeVar("M", ForecastModel, SaturationModel)

LATEST = "latest.json"
MODEL_FILE = "model.json"


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore")

    analysis: str
    output: str = ""


class _Summary(BaseModel):
    """Subconjunto de `summary.json` escrito por `emt-analysis run`."""

    model_config = ConfigDict(extra="ignore")

    generated_at: datetime | None = None
    results: list[_Result] = []


@dataclass(frozen=True)
class Loaded:
    bunching: ForecastModel | None
    saturation: SaturationModel | None
    generated_at: datetime | None

    @property
    def stops(self) -> list[str]:
        routes = list(self.bunching.routes if self.bunching else [])
        routes += list(self.saturation.routes if self.saturation else [])
        return sorted({route[1] for route in routes})


EMPTY = Loaded(None, None, None)


class ModelStore:
    """Modelos entrenados por `emt-analysis run` (reports/latest.json → <analysis>/model.json).

    Se recargan cuando cambia `latest.json`; nunca se usan modelos sintéticos.
    """

    def __init__(self, reports: Path) -> None:
        self._reports = reports
        self._signature: tuple[float, int] | None = None
        self._loaded = EMPTY

    @property
    def current(self) -> Loaded:
        latest = self._reports / LATEST
        try:
            stat = latest.stat()
        except OSError:
            self._signature = None
            self._loaded = EMPTY
            return self._loaded
        signature = (stat.st_mtime, stat.st_size)
        if signature != self._signature:
            self._signature = signature
            self._loaded = self._load(latest)
        return self._loaded

    def _load(self, latest: Path) -> Loaded:
        try:
            summary = _Summary.model_validate_json(latest.read_bytes())
        except (OSError, ValidationError) as exc:
            log.warning("bot.models_unreadable", path=str(latest), error=str(exc))
            return EMPTY
        outputs = {r.analysis: _resolve(self._reports, latest, r.output) for r in summary.results}
        bunching = _model(outputs.get("bunching"), ForecastModel)
        saturation = _model(outputs.get("saturation"), SaturationModel)
        log.info(
            "bot.models_loaded",
            generated_at=summary.generated_at,
            bunching=bunching is not None,
            saturation=saturation is not None,
        )
        return Loaded(bunching, saturation, summary.generated_at)


def _resolve(reports: Path, latest: Path, output: str) -> Path | None:
    if not output:
        return None
    path = Path(output)
    if path.is_dir():
        return path
    # summary.json written inside a container (/reports/...) read from another mount point.
    relative = Path(*path.parts[-2:]) if len(path.parts) >= 2 else path
    for base in (reports, latest.parent):
        if (base / relative).is_dir():
            return base / relative
    return None


def _model(folder: Path | None, kind: type[M]) -> M | None:
    if folder is None or not (folder / MODEL_FILE).is_file():
        return None
    try:
        model = kind.load(folder / MODEL_FILE)
    except (OSError, ValidationError, ValueError) as exc:
        log.warning("bot.model_invalid", path=str(folder / MODEL_FILE), error=str(exc))
        return None
    if model.source != "database":
        log.warning("bot.model_ignored_synthetic", path=str(folder / MODEL_FILE))
        return None
    return model
