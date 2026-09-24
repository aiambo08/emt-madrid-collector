"""Lectura tipada de `summary.json` escrito por `emt-impact` (para el bot y otros consumidores)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ChangeSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    metric: str
    before: float
    after: float
    delta: float
    ci_low: float
    ci_high: float
    p_value: float | None = None
    significant: bool


class RouteSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    line: str
    stop_id: str
    destination: str
    role: Literal["treated", "control"]
    changes: list[ChangeSummary]

    def change(self, metric: str) -> ChangeSummary | None:
        return next((c for c in self.changes if c.metric == metric), None)


class DiDSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    metric: str
    estimate: float
    ci_low: float
    ci_high: float
    significant: bool


class ImpactSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    source: Literal["synthetic", "database"]
    event: datetime
    start: datetime
    end_exclusive: datetime
    routes: list[RouteSummary]
    difference_in_differences: list[DiDSummary]

    @property
    def treated(self) -> list[RouteSummary]:
        return [r for r in self.routes if r.role == "treated"]

    @classmethod
    def load(cls, path: Path) -> ImpactSummary:
        return cls.model_validate_json(path.read_bytes())
