from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.preprocessing import StandardScaler

from emt_collector.bunching.domain import Example, Route
from emt_collector.bunching.features import FEATURE_NAMES as BASE_FEATURE_NAMES
from emt_collector.bunching.features import MADRID
from emt_collector.bunching.model import Metrics, baseline, metrics
from emt_collector.saturation.domain import Headway, SaturationParameters, Window
from emt_collector.saturation.headways import RouteReference, Threshold, build_reference

FEATURE_NAMES = (*BASE_FEATURE_NAMES, "reference_headway_minutes", "elapsed_ratio")
MIN_DAYS = 7
MIN_INSTANTS = 100
MIN_CLASS = 10


class RegressionMetrics(BaseModel):
    samples: int
    mae_minutes: float
    rmse_minutes: float
    median_error_minutes: float


class Evaluation(BaseModel):
    train_start: datetime
    train_end: datetime
    train_labels_end: datetime
    test_start: datetime
    test_end: datetime
    train_samples: int
    test_samples: int
    purged_samples: int
    saturation: Metrics
    saturation_baseline: Metrics
    wait: RegressionMetrics
    wait_baseline: RegressionMetrics


class Scored(BaseModel):
    """Una ventana de evaluación con sus predicciones y las del baseline."""

    route: tuple[str, str, str]
    at: datetime
    saturated: int
    wait_minutes: float
    probability: float
    baseline_probability: float
    expected_wait: float
    baseline_wait: float


class SaturationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Literal[1] = 1
    source: Literal["synthetic", "database"]
    trained_through: datetime
    parameters: SaturationParameters
    feature_names: tuple[str, ...] = FEATURE_NAMES
    routes: list[tuple[str, str, str]]
    reference: list[RouteReference]
    classifier_coefficients: list[float]
    classifier_intercept: float
    regressor_coefficients: list[float]
    regressor_intercept: float
    means: list[float]
    scales: list[float]
    threshold: float = Field(default=0.5, gt=0, lt=1)
    evaluation: Evaluation

    @model_validator(mode="after")
    def dimensions(self) -> SaturationModel:
        count = len(FEATURE_NAMES) + len(self.routes)
        if self.feature_names != FEATURE_NAMES:
            raise ValueError("Versión de features incompatible.")
        lengths = {
            len(self.classifier_coefficients),
            len(self.regressor_coefficients),
            len(self.means),
            len(self.scales),
        }
        if lengths != {count}:
            raise ValueError("Dimensiones de modelo incompatibles.")
        if any(scale <= 0 for scale in self.scales):
            raise ValueError("Escalas inválidas.")
        if self.trained_through.tzinfo is None:
            raise ValueError("trained_through requiere zona horaria.")
        if {(r.line, r.stop_id, r.destination) for r in self.reference} != set(self.routes):
            raise ValueError("Referencias y rutas no coinciden.")
        return self

    def thresholds(self) -> Threshold:
        return Threshold(self.reference, self.parameters)

    def _vector(self, route: Route, at: datetime, values: tuple[float, ...]) -> list[float]:
        key = (route.line, route.stop_id, route.destination)
        if key not in self.routes:
            raise ValueError("Ruta sin ejemplos de entrenamiento: no se extrapola.")
        if len(values) != len(BASE_FEATURE_NAMES) or not all(math.isfinite(x) for x in values):
            raise ValueError("Features inválidas.")
        reference = self.thresholds().reference_minutes(route, at)
        assert reference is not None
        return _extend(values, reference) + [float(key == item) for item in self.routes]

    def _score(self, vector: list[float], weights: list[float], intercept: float) -> float:
        return intercept + math.fsum(
            weight * (value - center) / scale
            for weight, value, center, scale in zip(
                weights, vector, self.means, self.scales, strict=True
            )
        )

    def probability(self, route: Route, at: datetime, values: tuple[float, ...]) -> float:
        score = self._score(
            self._vector(route, at, values),
            self.classifier_coefficients,
            self.classifier_intercept,
        )
        return 1 / (1 + math.exp(-max(-700.0, min(700.0, score))))

    def expected_wait(self, route: Route, at: datetime, values: tuple[float, ...]) -> float:
        score = self._score(
            self._vector(route, at, values),
            self.regressor_coefficients,
            self.regressor_intercept,
        )
        return max(0.0, score)

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> SaturationModel:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def _extend(values: tuple[float, ...], reference: float) -> list[float]:
    return [*values, reference, values[0] / reference if reference > 0 else 0.0]


def regression_metrics(targets: list[float], predictions: list[float]) -> RegressionMetrics:
    errors = [p - t for p, t in zip(predictions, targets, strict=True)]
    return RegressionMetrics(
        samples=len(errors),
        mae_minutes=sum(abs(e) for e in errors) / len(errors),
        rmse_minutes=math.sqrt(sum(e * e for e in errors) / len(errors)),
        median_error_minutes=median(errors),
    )


def wait_baseline(train: list[Window], test: list[Window]) -> list[float]:
    """Mediana de espera por ruta y hora local del instante, ajustada solo con entrenamiento."""
    groups: dict[tuple[Route, int], list[float]] = defaultdict(list)
    for row in train:
        groups[(row.route, row.at.astimezone(MADRID).hour)].append(row.wait_minutes)
    overall = median(row.wait_minutes for row in train)
    return [
        median(values)
        if (values := groups.get((row.route, row.at.astimezone(MADRID).hour)))
        else overall
        for row in test
    ]


def train_model(
    rows: list[Window],
    intervals: list[Headway],
    parameters: SaturationParameters,
    source: Literal["synthetic", "database"],
) -> tuple[SaturationModel, list[Scored]]:
    ordered = sorted(rows, key=lambda row: row.at)
    times = sorted({row.at for row in ordered})
    if len(times) < MIN_INSTANTS or (times[-1] - times[0]).total_seconds() < MIN_DAYS * 86400:
        raise ValueError(
            f"Histórico insuficiente: mínimo {MIN_DAYS} días y {MIN_INSTANTS} instantes "
            "etiquetados."
        )
    boundary = times[int(len(times) * 0.7)]
    train = [row for row in ordered if row.label_end < boundary]
    test = [row for row in ordered if row.at >= boundary]
    if not train:
        raise ValueError("Entrenamiento insuficiente: ninguna ventana etiquetada antes del corte.")
    train_labels_end = max(row.label_end for row in train)
    keys = sorted({(row.route.line, row.route.stop_id, row.route.destination) for row in train})
    if any((row.route.line, row.route.stop_id, row.route.destination) not in keys for row in test):
        raise ValueError(
            "Hay rutas nuevas en evaluación; selecciona un periodo con cobertura estable."
        )
    reference = [
        item
        for item in build_reference([h for h in intervals if h.available_at < train_labels_end])
        if (item.line, item.stop_id, item.destination) in keys
    ]
    if {(r.line, r.stop_id, r.destination) for r in reference} != set(keys):
        raise ValueError("Faltan intervalos de referencia para alguna ruta de entrenamiento.")
    threshold = Threshold(reference, parameters)

    def label(row: Window) -> int:
        value = threshold.is_saturated(row.headway)
        assert value is not None
        return int(value)

    train_labels = [label(row) for row in train]
    test_labels = [label(row) for row in test]
    if sum(train_labels) < MIN_CLASS or len(train) - sum(train_labels) < MIN_CLASS:
        raise ValueError(f"Entrenamiento insuficiente: mínimo {MIN_CLASS} ventanas de cada clase.")
    if len(set(test_labels)) < 2:
        raise ValueError("Evaluación insuficiente: el tramo final debe contener ambas clases.")

    def vector(row: Window) -> list[float]:
        reference_minutes = threshold.reference_minutes(row.route, row.at)
        assert reference_minutes is not None
        return _extend(row.values, reference_minutes) + [
            float((row.route.line, row.route.stop_id, row.route.destination) == key) for key in keys
        ]

    scaler = StandardScaler()
    matrix = scaler.fit_transform([vector(row) for row in train])
    test_matrix = scaler.transform([vector(row) for row in test])
    classifier = LogisticRegression(C=1.0, max_iter=2000, random_state=42)
    classifier.fit(matrix, train_labels)
    regressor = Ridge(alpha=1.0)
    regressor.fit(matrix, [row.wait_minutes for row in train])
    probabilities = [float(v) for v in classifier.predict_proba(test_matrix)[:, 1]]
    waits = [max(0.0, float(v)) for v in regressor.predict(test_matrix)]
    naive_probabilities = baseline(
        [_as_example(row, target) for row, target in zip(train, train_labels, strict=True)],
        [_as_example(row, target) for row, target in zip(test, test_labels, strict=True)],
    )
    naive_waits = wait_baseline(train, test)
    actual_waits = [row.wait_minutes for row in test]
    evaluation = Evaluation(
        train_start=train[0].at,
        train_end=train[-1].at,
        train_labels_end=train_labels_end,
        test_start=test[0].at,
        test_end=test[-1].at,
        train_samples=len(train),
        test_samples=len(test),
        purged_samples=len(rows) - len(train) - len(test),
        saturation=metrics(test_labels, probabilities),
        saturation_baseline=metrics(test_labels, naive_probabilities),
        wait=regression_metrics(actual_waits, waits),
        wait_baseline=regression_metrics(actual_waits, naive_waits),
    )
    model = SaturationModel(
        source=source,
        trained_through=max(row.label_end for row in train),
        parameters=parameters,
        routes=keys,
        reference=reference,
        classifier_coefficients=[float(x) for x in classifier.coef_[0]],
        classifier_intercept=float(classifier.intercept_[0]),
        regressor_coefficients=[float(x) for x in regressor.coef_],
        regressor_intercept=float(regressor.intercept_),
        means=[float(x) for x in scaler.mean_],
        scales=[float(x) for x in scaler.scale_],
        evaluation=evaluation,
    )
    scored = [
        Scored(
            route=(row.route.line, row.route.stop_id, row.route.destination),
            at=row.at,
            saturated=target,
            wait_minutes=row.wait_minutes,
            probability=probability,
            baseline_probability=naive,
            expected_wait=wait,
            baseline_wait=naive_wait,
        )
        for row, target, probability, naive, wait, naive_wait in zip(
            test, test_labels, probabilities, naive_probabilities, waits, naive_waits, strict=True
        )
    ]
    return model, scored


def _as_example(row: Window, target: int) -> Example:
    return Example(row.route, row.at, row.label_end, row.values, target)
