from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.preprocessing import StandardScaler

from emt_collector.bunching.domain import Example, Parameters, Route
from emt_collector.bunching.features import FEATURE_NAMES, MADRID


class Metrics(BaseModel):
    samples: int
    positives: int
    prevalence: float
    precision: float
    recall: float
    average_precision: float | None
    roc_auc: float | None
    brier: float


class Evaluation(BaseModel):
    train_start: datetime
    train_end: datetime
    train_labels_end: datetime
    test_start: datetime
    test_end: datetime
    train_samples: int
    test_samples: int
    purged_samples: int
    model: Metrics
    baseline: Metrics


class ForecastModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Literal[1] = 1
    source: Literal["synthetic", "database"]
    trained_through: datetime
    parameters: Parameters
    feature_names: tuple[str, ...] = FEATURE_NAMES
    routes: list[tuple[str, str, str]]
    coefficients: list[float]
    intercept: float
    means: list[float]
    scales: list[float]
    threshold: float = Field(default=0.5, gt=0, lt=1)
    evaluation: Evaluation

    @model_validator(mode="after")
    def dimensions(self) -> ForecastModel:
        count = len(FEATURE_NAMES) + len(self.routes)
        if self.feature_names != FEATURE_NAMES:
            raise ValueError("Versión de features incompatible.")
        if not len(self.coefficients) == len(self.means) == len(self.scales) == count:
            raise ValueError("Dimensiones de modelo incompatibles.")
        if any(scale <= 0 for scale in self.scales):
            raise ValueError("Escalas inválidas.")
        if self.trained_through.tzinfo is None:
            raise ValueError("trained_through requiere zona horaria.")
        return self

    def probability(self, route: Route, values: tuple[float, ...]) -> float:
        key = (route.line, route.stop_id, route.destination)
        if key not in self.routes:
            raise ValueError("Ruta sin ejemplos de entrenamiento: no se extrapola.")
        if len(values) != len(FEATURE_NAMES) or not all(math.isfinite(x) for x in values):
            raise ValueError("Features inválidas.")
        vector = list(values) + [float(key == item) for item in self.routes]
        score = self.intercept + math.fsum(
            weight * (value - center) / scale
            for weight, value, center, scale in zip(
                self.coefficients, vector, self.means, self.scales, strict=True
            )
        )
        return 1 / (1 + math.exp(-max(-700.0, min(700.0, score))))

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ForecastModel:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def metrics(targets: list[int], probabilities: list[float]) -> Metrics:
    predictions = [int(p >= 0.5) for p in probabilities]
    both = len(set(targets)) == 2
    return Metrics(
        samples=len(targets),
        positives=sum(targets),
        prevalence=sum(targets) / len(targets),
        precision=float(precision_score(targets, predictions, zero_division=0)),
        recall=float(recall_score(targets, predictions, zero_division=0)),
        average_precision=float(average_precision_score(targets, probabilities)) if both else None,
        roc_auc=float(roc_auc_score(targets, probabilities)) if both else None,
        brier=float(brier_score_loss(targets, probabilities)),
    )


def baseline(train: list[Example], test: list[Example]) -> list[float]:
    groups: dict[tuple[str, int], list[int]] = defaultdict(list)
    prior = sum(row.target for row in train) / len(train)
    for row in train:
        groups[(row.route.line, row.at.astimezone(MADRID).hour)].append(row.target)
    return [
        (sum(labels) + 10 * prior) / (len(labels) + 10)
        if (labels := groups.get((row.route.line, row.at.astimezone(MADRID).hour)))
        else prior
        for row in test
    ]


def train_model(
    rows: list[Example], parameters: Parameters, source: Literal["synthetic", "database"]
) -> tuple[ForecastModel, list[tuple[Example, float, float]]]:
    ordered = sorted(rows, key=lambda row: row.at)
    times = sorted({row.at for row in ordered})
    if len(times) < 100 or (times[-1] - times[0]).total_seconds() < 7 * 86400:
        raise ValueError("Histórico insuficiente: mínimo 7 días y 100 instantes etiquetados.")
    boundary = times[int(len(times) * 0.7)]
    train = [row for row in ordered if row.label_end < boundary]
    test = [row for row in ordered if row.at >= boundary]
    if sum(row.target for row in train) < 10 or sum(1 - row.target for row in train) < 10:
        raise ValueError("Entrenamiento insuficiente: mínimo 10 ventanas de cada clase.")
    if len({row.target for row in test}) < 2:
        raise ValueError("Evaluación insuficiente: el tramo final debe contener ambas clases.")
    keys = sorted({(row.route.line, row.route.stop_id, row.route.destination) for row in train})
    if any((row.route.line, row.route.stop_id, row.route.destination) not in keys for row in test):
        raise ValueError(
            "Hay rutas nuevas en evaluación; selecciona un periodo con cobertura estable."
        )

    def vector(row: Example) -> list[float]:
        return list(row.values) + [
            float((row.route.line, row.route.stop_id, row.route.destination) == key) for key in keys
        ]

    scaler = StandardScaler()
    matrix = scaler.fit_transform([vector(row) for row in train])
    classifier = LogisticRegression(C=1.0, max_iter=2000, random_state=42)
    classifier.fit(matrix, [row.target for row in train])
    probabilities = [
        float(value)
        for value in classifier.predict_proba(scaler.transform([vector(row) for row in test]))[:, 1]
    ]
    naive = baseline(train, test)
    targets = [row.target for row in test]
    evaluation = Evaluation(
        train_start=train[0].at,
        train_end=train[-1].at,
        train_labels_end=max(row.label_end for row in train),
        test_start=test[0].at,
        test_end=test[-1].at,
        train_samples=len(train),
        test_samples=len(test),
        purged_samples=len(rows) - len(train) - len(test),
        model=metrics(targets, probabilities),
        baseline=metrics(targets, naive),
    )
    model = ForecastModel(
        source=source,
        trained_through=max(row.label_end for row in train),
        parameters=parameters,
        routes=keys,
        coefficients=[float(x) for x in classifier.coef_[0]],
        intercept=float(classifier.intercept_[0]),
        means=[float(x) for x in scaler.mean_],
        scales=[float(x) for x in scaler.scale_],
        evaluation=evaluation,
    )
    return model, list(zip(test, probabilities, naive, strict=True))
