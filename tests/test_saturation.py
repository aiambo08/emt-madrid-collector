from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine

from emt_collector.bunching.detector import build_series
from emt_collector.bunching.domain import Gap, Observation, Parameters, Route
from emt_collector.saturation.cli import main
from emt_collector.saturation.demo import synthetic_history
from emt_collector.saturation.domain import Headway, SaturationParameters, Window
from emt_collector.saturation.headways import (
    Threshold,
    build_reference,
    headways,
    windows,
)
from emt_collector.saturation.model import FEATURE_NAMES, SaturationModel, train_model
from emt_collector.saturation.report import predict

START = datetime(2026, 1, 12, 8, tzinfo=timezone.utc)
ROUTE = Route("27", "100", "NORTE")
PARAMETERS = SaturationParameters()


def at(minute: int) -> datetime:
    return START + timedelta(minutes=minute)


def observation(minute: int, bus: int, eta: int = 0) -> Observation:
    return Observation(ROUTE, bus, at(minute), at(minute), eta, eta * 2, False)


def history(departures: list[int], until: int = 240) -> list[Observation]:
    rows = [observation(minute, 999, 3600) for minute in range(until + 1)]
    rows.extend(observation(minute, bus) for bus, minute in enumerate(departures, 1))
    return rows


def test_headways_skip_repeated_bus_and_require_coverage() -> None:
    rows = history([0, 10, 25, 40])
    rows.append(observation(30, 3))
    series = build_series(rows, [], PARAMETERS)[0]
    found = headways(series)
    assert [(h.previous_bus, h.bus, h.minutes) for h in found] == [
        (1, 2, 10.0),
        (2, 3, 15.0),
        (3, 4, 15.0),
    ]
    assert all(h.available_at >= h.end for h in found)
    gapped = build_series(history([0, 10, 25, 40]), [Gap(at(17), "100", "27")], PARAMETERS)[0]
    assert [(h.previous_bus, h.bus) for h in headways(gapped)] == [(1, 2), (3, 4)]


def test_reference_uses_hourly_median_with_route_fallback() -> None:
    departures = [0, 8, 16, 24, 32, 40, 48, 56, 60, 75, 90, 105, 118, 140]
    series = build_series(history(departures), [], PARAMETERS)[0]
    reference = build_reference(headways(series))
    assert len(reference) == 1 and reference[0].samples == 13
    hourly = reference[0].hourly_minutes
    assert hourly[9] == 8.0
    assert hourly[10] == 15.0
    assert hourly[11] == hourly[3] == 8.0
    threshold = Threshold(reference, PARAMETERS)
    assert threshold.minutes(ROUTE, at(0)) == 12.0
    assert threshold.minutes(ROUTE, at(70)) == 22.5
    assert threshold.minutes(Route("45", "1", "SUR"), at(0)) is None
    saturated = threshold.saturated(headways(series))
    assert [(item.headway.previous_bus, item.headway.bus) for item in saturated] == [(13, 14)]
    assert saturated[0].headway.minutes == 22.0
    assert saturated[0].threshold_minutes == 12.0


def test_windows_are_causal_and_wait_matches_next_real_arrival() -> None:
    departures = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 100, 105]
    series = build_series(history(departures), [], PARAMETERS)[0]
    intervals = headways(series)
    rows = windows(series, intervals, series.times[-1])
    assert rows and all(row.at < row.headway.end <= row.label_end for row in rows)
    assert all(row.label_end - row.headway.end == timedelta(seconds=180) for row in rows)
    inside = [row for row in rows if at(75) < row.at < at(100)]
    assert [round(row.wait_minutes) for row in inside] == [20, 15, 10, 5]
    assert all(row.headway.bus == 17 for row in inside)
    truncated = windows(series, intervals, at(90))
    assert truncated and all(row.label_end <= at(90) for row in truncated)
    assert not any(at(75) < row.at for row in truncated)
    with pytest.raises(TypeError, match="SaturationParameters"):
        windows(build_series(history(departures), [], Parameters())[0], intervals, at(1))


@pytest.fixture(scope="module")
def demo_rows() -> tuple[list[Window], list[Headway], SaturationParameters]:
    observations, gaps = synthetic_history(days=12, seed=3)
    rows: list[Window] = []
    intervals: list[Headway] = []
    for series in build_series(observations, gaps, PARAMETERS):
        found = headways(series)
        intervals.extend(found)
        rows.extend(windows(series, found, series.times[-1]))
    return rows, intervals, PARAMETERS


def test_temporal_training_reference_and_export(
    demo_rows: tuple[list[Window], list[Headway], SaturationParameters], tmp_path: Path
) -> None:
    rows, intervals, parameters = demo_rows
    model, scored = train_model(rows, intervals, parameters, "synthetic")
    ev = model.evaluation
    assert ev.train_labels_end < ev.test_start
    assert ev.test_samples == len(scored)
    assert ev.saturation.brier < ev.saturation_baseline.brier
    assert ev.wait.mae_minutes < ev.wait_baseline.mae_minutes
    assert model.feature_names == FEATURE_NAMES
    training_intervals = [item for item in intervals if item.available_at < ev.train_labels_end]
    assert model.reference == build_reference(training_intervals)
    model.save(tmp_path / "model.json")
    loaded = SaturationModel.load(tmp_path / "model.json")
    for row in scored[:10]:
        route = Route(*row.route)
        window = next(w for w in rows if w.route == route and w.at == row.at)
        assert loaded.probability(route, row.at, window.values) == pytest.approx(row.probability)
        assert loaded.expected_wait(route, row.at, window.values) == pytest.approx(
            row.expected_wait
        )
    with pytest.raises(ValueError, match="Ruta sin ejemplos"):
        loaded.probability(ROUTE, ev.test_end, rows[0].values)
    with pytest.raises(ValueError, match="antes del final"):
        predict([], loaded, START)
    later = ev.test_end + timedelta(days=30)
    assert all(p.status == "insufficient_coverage" for p in predict([], loaded, later))
    payload = json.loads((tmp_path / "model.json").read_text())
    payload["scales"] = [0] * len(model.scales)
    with pytest.raises(ValueError, match="Escalas inválidas"):
        SaturationModel.model_validate(payload)


def test_insufficient_history_never_trains(
    demo_rows: tuple[list[Window], list[Headway], SaturationParameters],
) -> None:
    rows, intervals, parameters = demo_rows
    with pytest.raises(ValueError, match="Histórico insuficiente"):
        train_model([], intervals, parameters, "database")
    short = [row for row in rows if row.at < rows[0].at + timedelta(days=5)]
    with pytest.raises(ValueError, match="Histórico insuficiente"):
        train_model(short, intervals, parameters, "database")
    loose = SaturationParameters(ratio=50)
    with pytest.raises(ValueError, match="Entrenamiento insuficiente"):
        train_model(rows, intervals, loose, "database")


def test_demo_cli_writes_report_and_refuses_existing_directory(tmp_path: Path) -> None:
    output = tmp_path / "demo"
    assert main(["demo", "--days", "12", "--seed", "3", "--output", str(output)]) == 0
    summary = json.loads((output / "summary.json").read_text())
    assert summary["source"] == "synthetic"
    assert summary["saturated_headways"] > 0
    assert summary["evaluation"]["saturation"]["positives"] > 0
    report = (output / "report.html").read_text(encoding="utf-8")
    assert "DEMO SINTÉTICA" in report
    assert "<!--BUNCHING_CONTENT-->" not in report
    assert "Bus bunching · EMT Madrid" not in report
    assert "https://" not in report and "<script src" not in report
    predictions = json.loads((output / "predictions.json").read_text())
    assert {p["status"] for p in predictions} <= {"ok", "insufficient_coverage"}
    assert (output / "headways.json").exists() and (output / "backtest.csv").exists()
    assert main(["demo", "--output", str(output)]) == 2
    assert main(["predict", "--model", str(output / "model.json")]) == 2


def test_cli_empty_database_exports_report_without_model(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr("emt_collector.saturation.cli.make_engine", lambda _: engine)
    output = tmp_path / "report"
    assert main(["analyze", "--start", START.isoformat(), "--output", str(output)]) == 3
    summary = json.loads((output / "summary.json").read_text())
    assert summary["headways"] == summary["labeled_windows"] == 0
    assert summary["training_error"]
    assert not (output / "model.json").exists()
    assert "HISTÓRICO DE LA BASE DE DATOS" in (output / "report.html").read_text()
