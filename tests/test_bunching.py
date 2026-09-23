from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from emt_collector.bunching.cli import main, timestamp
from emt_collector.bunching.data import load_history
from emt_collector.bunching.demo import synthetic_history
from emt_collector.bunching.detector import build_series, detect
from emt_collector.bunching.domain import Example, Gap, Observation, Parameters, Route
from emt_collector.bunching.features import examples, features
from emt_collector.bunching.model import ForecastModel, train_model
from emt_collector.bunching.report import predict, table
from emt_collector.db.models import ArrivalEstimate, CollectionCycle, CollectionGap

START = datetime(2026, 1, 12, 8, tzinfo=timezone.utc)
ROUTE = Route("27", "100", "NORTE")
PARAMETERS = Parameters()


def at(minute: int) -> datetime:
    return START + timedelta(minutes=minute)


def observation(minute: int, bus: int, eta: int = 0) -> Observation:
    return Observation(ROUTE, bus, at(minute), at(minute), eta, eta * 2, False)


def history(departures: list[int], until: int = 120) -> list[Observation]:
    rows = [observation(minute, 999, 3600) for minute in range(until + 1)]
    rows.extend(observation(minute, bus) for bus, minute in enumerate(departures, 1))
    return rows


def test_three_distinct_buses_after_twenty_minutes() -> None:
    rows = history([0, 20, 21, 23, 30])
    series = build_series(rows + rows, [], PARAMETERS)[0]
    events = detect(series)
    assert len(events) == 1
    assert events[0].bus_ids == (2, 3, 4)
    assert events[0].gap_seconds == 1200
    assert events[0].span_seconds == 180
    assert len(series.passages) == 5


@pytest.mark.parametrize("departures", [[0, 19, 20, 21], [0, 20, 24, 25], [0, 20, 21]])
def test_regular_or_incomplete_groups_are_not_events(departures: list[int]) -> None:
    assert not detect(build_series(history(departures), [], PARAMETERS)[0])


def test_stationary_bus_and_reused_bus_id() -> None:
    rows = history([])
    rows += [observation(minute, 7) for minute in range(30)]
    rows.append(observation(50, 7))
    series = build_series(rows, [], PARAMETERS)[0]
    assert [p.at for p in series.passages] == [at(0), at(50)]
    assert not detect(series)


@pytest.mark.parametrize("gap", [Gap(at(10)), Gap(at(10), "100"), Gap(at(10), "100", "27")])
def test_explicit_gaps_prevent_false_bunching(gap: Gap) -> None:
    assert not detect(build_series(history([0, 20, 21, 22]), [gap], PARAMETERS)[0])


def test_missing_polling_and_gaps_at_other_stops() -> None:
    rows = history([0, 20, 21, 22])
    series = build_series(rows, [Gap(at(10), "another-stop")], PARAMETERS)[0]
    assert len(detect(series)) == 1
    missing = [row for row in rows if row.sample_ts != at(10)]
    assert not detect(build_series(missing, [], PARAMETERS)[0])
    assert not detect(build_series(history([20, 21, 22]), [], PARAMETERS)[0])


@pytest.mark.parametrize(
    "route", [Route("45", "100", "NORTE"), Route("27", "100", "SUR"), Route("27", "200", "NORTE")]
)
def test_directions_lines_and_stops_do_not_mix(route: Route) -> None:
    rows = history([0, 20, 21])
    rows.extend(replace(row, route=route) for row in history([0, 22]))
    assert not any(detect(series) for series in build_series(rows, [], PARAMETERS))


@pytest.mark.parametrize(
    "invalid",
    [
        {"eta": None},
        {"eta": 999_999},
        {"eta": -1},
        {"distance_m": None},
        {"distance_m": -1},
        {"distance_m": 151},
        {"is_head": True},
    ],
)
def test_invalid_arrivals_never_become_passages(invalid: dict[str, int | bool | None]) -> None:
    rows = history([0, 20, 21])
    rows.append(replace(observation(22, 10), **invalid))
    assert not detect(build_series(rows, [], PARAMETERS)[0])


def test_unknown_destination_and_stale_samples_are_excluded() -> None:
    unknown = replace(observation(0, 1), route=Route("27", "100", ""))
    stale = replace(observation(0, 2), ingested_at=at(3))
    assert build_series([unknown, stale], [], PARAMETERS) == []


def test_features_are_causal_even_with_future_outages_and_delayed_ingestion() -> None:
    rows = history(list(range(0, 66, 8)), until=120)
    rows = [row for row in rows if not at(66) <= row.sample_ts < at(90)]
    rows.append(replace(observation(64, 500), ingested_at=at(66)))
    full = build_series(rows, [], PARAMETERS)[0]
    prefix = build_series([row for row in rows if row.available_at <= at(65)], [], PARAMETERS)[0]
    assert features(full, at(65)) is not None
    assert features(full, at(65)) == features(prefix, at(65))
    assert all(p.bus_id != 500 for p in prefix.passages)
    assert features(full, at(68)) is None


def test_labels_look_forward_and_require_complete_future() -> None:
    rows = history([0, 10, 20, 30, 40, 50, 80, 81, 82, 90, 100])
    series = build_series(rows, [], PARAMETERS)[0]
    samples = {row.at: row for row in examples(series, detect(series), at(120))}
    assert samples[at(60)].target == 0
    assert samples[at(65)].target == 1
    assert samples[at(75)].target == 1
    assert samples[at(80)].target == 0
    assert all(row.label_end <= at(120) for row in samples.values())
    gapped = build_series(rows, [Gap(at(85))], PARAMETERS)[0]
    assert at(65) not in {row.at for row in examples(gapped, detect(gapped), at(120))}


def test_calendar_uses_madrid_and_dst() -> None:
    shift = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc) - at(60)
    rows = [
        replace(row, sample_ts=row.sample_ts + shift, ingested_at=row.ingested_at + shift)
        for row in history(list(range(0, 121, 8)))
    ]
    values = features(build_series(rows, [], PARAMETERS)[0], at(60) + shift)
    assert values is not None
    assert values[8] == pytest.approx(math.sin(3.5 * 2 * math.pi / 24))
    assert values[-1] == 1


def test_future_eta_is_not_yet_a_passage_or_complete_event() -> None:
    rows = history([0, 20, 21], until=22)
    rows.append(observation(22, 10, eta=60))
    series = build_series(rows, [], PARAMETERS)[0]
    assert not detect(series)
    rows.append(observation(23, 999, eta=3600))
    assert len(detect(build_series(rows, [], PARAMETERS)[0])) == 1


@pytest.fixture(scope="module")
def training_rows() -> list[Example]:
    observations, gaps = synthetic_history(days=14)
    result = []
    for series in build_series(observations, gaps, PARAMETERS):
        result.extend(examples(series, detect(series), series.times[-1]))
    return result


def test_temporal_evaluation_export_and_future_data_do_not_change_training(
    training_rows: list[Example],
    tmp_path: Path,
) -> None:
    model, holdout = train_model(training_rows, PARAMETERS, "synthetic")
    evaluation = model.evaluation
    assert evaluation.train_labels_end < evaluation.test_start
    assert evaluation.purged_samples > 0
    assert evaluation.test_samples == len(holdout)
    assert evaluation.model.brier < evaluation.baseline.brier
    model.save(tmp_path / "model.json")
    loaded = ForecastModel.load(tmp_path / "model.json")
    for row, risk, _ in holdout[:10]:
        assert loaded.probability(row.route, row.values) == pytest.approx(risk)
    changed = [
        replace(row, values=tuple(value + 1000 for value in row.values), target=1 - row.target)
        if row.at >= evaluation.test_start
        else row
        for row in training_rows
    ]
    retrained, _ = train_model(changed, PARAMETERS, "synthetic")
    assert retrained.coefficients == model.coefficients
    assert retrained.means == model.means
    assert retrained.scales == model.scales
    with pytest.raises(ValueError, match="Ruta sin ejemplos"):
        loaded.probability(ROUTE, training_rows[0].values)
    with pytest.raises(ValueError, match="antes del final"):
        predict([], loaded, START)
    assert all(p.probability is None for p in predict([], loaded, at(30 * 24 * 60)))
    assert main(["predict", "--model", str(tmp_path / "model.json")]) == 2


def test_insufficient_history_never_trains(training_rows: list[Example]) -> None:
    with pytest.raises(ValueError, match="Histórico insuficiente"):
        train_model([], PARAMETERS, "database")
    with pytest.raises(ValueError, match="Entrenamiento insuficiente"):
        train_model([replace(row, target=0) for row in training_rows], PARAMETERS, "database")
    with pytest.raises(ValueError, match="Escalas inválidas"):
        model, _ = train_model(training_rows, PARAMETERS, "synthetic")
        payload = model.model_dump()
        payload["scales"] = [0] * len(model.scales)
        ForecastModel.model_validate(payload)


def test_database_reads_time_bounds_normalizes_ids_and_loads_gaps(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        for minute, stop in ((0, "100"), (1, "100"), (2, "200"), (3, "100")):
            session.add(
                ArrivalEstimate(
                    stop_id=stop,
                    line="027",
                    bus_id=1,
                    sample_ts=at(minute),
                    ingested_at=at(minute),
                    estimate_seconds=0,
                    distance_m=0,
                    destination="NORTE",
                )
            )
        session.add(CollectionGap(occurred_at=at(1), scope="stop", kind="network", stop_id="100"))
    rows, gaps = load_history(engine, at(1), at(3), stops=["100"])
    assert len(rows) == len(gaps) == 1
    assert rows[0].route == ROUTE
    assert rows[0].sample_ts == at(1)
    with pytest.raises(ValueError, match="Demasiadas muestras"):
        load_history(engine, at(0), at(4), max_rows=2)
    with pytest.raises(ValueError, match="posterior"):
        load_history(engine, at(1), at(0))


def test_cycle_completion_controls_sample_availability(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        for ident, finished in ((1, at(2)), (2, None)):
            session.add(
                CollectionCycle(
                    id=ident,
                    started_at=at(0),
                    finished_at=finished,
                    status="ok" if finished else "running",
                )
            )
            session.add(
                ArrivalEstimate(
                    stop_id="100",
                    line="27",
                    bus_id=ident,
                    sample_ts=at(0),
                    ingested_at=at(0),
                    estimate_seconds=0,
                    distance_m=0,
                    destination="NORTE",
                    cycle_id=ident,
                )
            )
    assert load_history(engine, at(0), at(1))[0] == []
    rows, _ = load_history(engine, at(0), at(3))
    assert len(rows) == 1
    assert rows[0].ingested_at == at(0)
    assert rows[0].available_at == at(2)


def test_cli_empty_database_exports_report_without_model(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr("emt_collector.bunching.cli.make_engine", lambda _: engine)
    output = tmp_path / "report"
    args = ["analyze", "--start", START.isoformat(), "--output", str(output)]
    assert main(args) == 3
    summary = json.loads((output / "summary.json").read_text())
    assert summary["events"] == summary["labeled_windows"] == 0
    assert summary["training_error"]
    assert not (output / "model.json").exists()
    report = (output / "report.html").read_text()
    assert "HISTÓRICO DE LA BASE DE DATOS" in report
    assert "<!--BUNCHING_CONTENT-->" not in report
    assert main(args) == 2
    assert timestamp("2026-01-12T09:00:00+01:00") == START
    assert "&lt;script&gt;" in table(["header"], [["<script>"]], "caption")
