from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from sqlalchemy import Engine

from emt_collector.bunching.detector import build_series
from emt_collector.bunching.domain import Route
from emt_collector.frequency.cli import main
from emt_collector.frequency.demo import synthetic_history
from emt_collector.frequency.domain import FrequencyParameters, HourlyService, RoutePlan, Skipped
from emt_collector.frequency.optimizer import allocate, plan_route
from emt_collector.frequency.service import cycle_minutes, hourly_service, read_demand_csv
from emt_collector.saturation.headways import Threshold, build_reference, headways
from tests.test_saturation import START

ROUTE = Route("27", "100", "NORTE")


def service(hour: int, headway: float, cv: float, weight: float) -> HourlyService:
    return HourlyService(ROUTE, hour, 10, 5, headway, cv, 0, weight)


def test_expected_wait_grows_with_irregularity() -> None:
    regular = service(8, 10, 0.0, 1)
    bunched = service(8, 10, 0.5, 1)
    assert regular.expected_wait_minutes == regular.regular_wait_minutes == 5
    assert bunched.expected_wait_minutes == pytest.approx(6.25)


def test_allocation_keeps_bus_hours_and_moves_buses_to_demand() -> None:
    parameters = FrequencyParameters(min_planned_headway_minutes=2)
    rows = [service(8, 6, 0.3, 10), service(11, 6, 0.3, 1)]
    plans = allocate(rows, cycle=60, parameters=parameters)
    proposed = [plan.proposed_buses for plan in plans]
    assert sum(proposed) <= sum(plan.current_buses for plan in plans) == 20
    assert proposed[0] > proposed[1] >= 3
    assert all(
        parameters.min_planned_headway_minutes
        <= plan.proposed_headway_minutes
        <= parameters.max_planned_headway_minutes
        for plan in plans
    )
    assert sum(plan.weighted_saving_minutes for plan in plans) > 0


def test_uniform_weights_equalise_headways() -> None:
    rows = [service(8, 4, 0.2, 1), service(11, 12, 0.2, 1)]
    plans = allocate(rows, cycle=60, parameters=FrequencyParameters())
    assert [plan.proposed_buses for plan in plans] == [10, 10]


def test_cycle_and_hourly_service_from_synthetic_history() -> None:
    parameters = FrequencyParameters(min_days=3)
    observations, gaps = synthetic_history(days=4, seed=1)
    series = build_series(observations, gaps, parameters)
    intervals = {item.route: headways(item) for item in series}
    threshold = Threshold(
        build_reference([row for rows in intervals.values() for row in rows]), parameters
    )
    line_27 = next(item for item in series if item.route.line == "27")
    cycle, samples = cycle_minutes(line_27, parameters)
    assert cycle is not None and 90 <= cycle <= 160 and samples > 100
    rows = hourly_service(line_27, intervals[line_27.route], threshold, parameters, None)
    assert {row.hour for row in rows} <= set(range(6, 23))
    assert all(row.headways >= parameters.min_hour_samples and row.weight > 0 for row in rows)
    assert isinstance(plan_route(line_27, rows, parameters), RoutePlan)
    strict = FrequencyParameters(min_days=10)
    assert hourly_service(line_27, intervals[line_27.route], threshold, strict, None) == []
    assert isinstance(plan_route(line_27, [], strict), Skipped)


def test_demand_csv_validation(tmp_path: Path) -> None:
    path = tmp_path / "demand.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["line", "hour", "weight"])
        writer.writerow(["", "8", "5"])
        writer.writerow(["27", "8", "9"])
    assert read_demand_csv(path) == {(None, 8): 5.0, ("27", 8): 9.0}
    path.write_text("hour,weight\n25,1\n")
    with pytest.raises(ValueError, match="inválida"):
        read_demand_csv(path)
    path.write_text("foo,bar\n1,2\n")
    with pytest.raises(ValueError, match="columnas"):
        read_demand_csv(path)


def test_demo_cli_writes_report_and_refuses_existing_directory(tmp_path: Path) -> None:
    output = tmp_path / "demo"
    assert main(["demo", "--days", "6", "--seed", "3", "--output", str(output)]) == 0
    summary = json.loads((output / "summary.json").read_text())
    assert summary["source"] == "synthetic" and len(summary["routes"]) == 2
    for route in summary["routes"]:
        assert route["proposed_wait_minutes"] <= route["current_wait_minutes"]
        assert route["regular_wait_minutes"] < route["current_wait_minutes"]
    report = (output / "report.html").read_text(encoding="utf-8")
    assert "DEMO SINTÉTICA" in report and "<!--BUNCHING_CONTENT-->" not in report
    assert "https://" not in report and "<script src" not in report
    assert (output / "plan.csv").exists() and (output / "demand.csv").exists()
    assert main(["demo", "--output", str(output)]) == 2


def test_cli_empty_database_reports_no_routes(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr("emt_collector.frequency.cli.make_engine", lambda _: engine)
    output = tmp_path / "report"
    assert main(["analyze", "--start", START.isoformat(), "--output", str(output)]) == 3
    summary = json.loads((output / "summary.json").read_text())
    assert summary["routes"] == [] and summary["parameters"]["demand_mode"] == "proxy"
    report = (output / "report.html").read_text()
    assert "HISTÓRICO DE LA BASE DE DATOS" in report
    assert "Ninguna ruta con datos suficientes" in report
