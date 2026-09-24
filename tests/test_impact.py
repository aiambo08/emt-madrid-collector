from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine

from emt_collector.bunching.detector import build_series
from emt_collector.impact.analysis import analyze, clip_windows, compare, window_metrics
from emt_collector.impact.cli import main
from emt_collector.impact.demo import CONTROL_LINE, TREATED_LINE, synthetic_history
from emt_collector.impact.domain import ImpactParameters, Period, WindowMetrics
from emt_collector.impact.summary import ImpactSummary
from emt_collector.saturation.headways import Threshold, build_reference, headways
from tests.test_saturation import ROUTE, START, at, history

PARAMETERS = ImpactParameters(bootstrap_samples=200, min_headways=3, min_days=1)


def metrics(period: Period, values: list[float], saturated: list[bool]) -> WindowMetrics:
    return WindowMetrics(
        ROUTE,
        period,
        START,
        START + timedelta(days=1),
        tuple(values),
        tuple(saturated),
        (1, 0),
        {},
    )


def test_window_metrics_formulas() -> None:
    window = metrics("before", [10, 10, 10, 30], [False, False, False, True])
    assert window.mean_headway_minutes == 15
    # E[H²]/2E[H] = (3·100 + 900) / (2·60)
    assert window.expected_wait_minutes == pytest.approx(10)
    assert window.saturation_rate == 0.25
    assert window.episodes_per_day == 0.5 and window.days == 2
    assert window.cv == pytest.approx(np.std([10, 10, 10, 30]) / 15)


def test_window_metrics_splits_intervals_and_episodes_by_window() -> None:
    rows = history([0, 10, 20, 60, 70, 80], until=100)
    series = build_series(rows, [], PARAMETERS)[0]
    intervals = headways(series)
    threshold = Threshold(build_reference(intervals), PARAMETERS)
    before = window_metrics(series, intervals, [], threshold, "before", at(0), at(30))
    after = window_metrics(series, intervals, [], threshold, "after", at(30), at(100))
    assert before.headway_minutes == (10.0, 10.0)
    assert after.headway_minutes == (40.0, 10.0, 10.0)
    assert before.days == after.days == 1


def test_compare_is_deterministic_and_detects_a_real_change() -> None:
    before = metrics("before", [10.0 + (i % 3) for i in range(60)], [False] * 60)
    after = metrics("after", [6.0 + (i % 3) for i in range(60)], [False] * 60)
    changes, deltas = compare(before, after, PARAMETERS, np.random.default_rng(0))
    again, _ = compare(before, after, PARAMETERS, np.random.default_rng(0))
    assert changes == again
    headway = changes[0]
    assert headway.metric == "mean_headway_minutes"
    assert headway.delta == pytest.approx(-4)
    assert headway.ci_low < headway.ci_high < 0 and headway.significant
    assert headway.p_value is not None and headway.p_value < 0.001
    assert set(deltas) == {c.metric for c in changes}
    assert all(len(values) == PARAMETERS.bootstrap_samples for values in deltas.values())
    wait = changes[1]
    assert wait.metric == "expected_wait_minutes" and wait.p_value is None and wait.significant


def test_compare_reports_no_change_for_identical_windows() -> None:
    values = [8.0 + (i % 5) for i in range(50)]
    before = metrics("before", values, [False] * 50)
    after = metrics("after", values, [False] * 50)
    changes, _ = compare(before, after, PARAMETERS, np.random.default_rng(1))
    for change in changes:
        assert change.ci_low <= 0 <= change.ci_high and not change.significant
    assert changes[0].p_value == pytest.approx(1.0)


def test_analyze_uses_controls_for_difference_in_differences_and_skips_thin_routes() -> None:
    observations, gaps, event = synthetic_history(days=6, seed=2)
    start = min(row.sample_ts for row in observations)
    end = max(row.available_at for row in observations) + timedelta(seconds=1)
    parameters = ImpactParameters(bootstrap_samples=200)
    series = build_series(observations, gaps, parameters)
    impacts, did, skipped = analyze(
        series,
        event,
        start,
        end,
        parameters,
        lambda route: "control" if route.line == CONTROL_LINE else "treated",
    )
    assert skipped == []
    by_line = {impact.route.line: impact for impact in impacts}
    treated, control = by_line[TREATED_LINE], by_line[CONTROL_LINE]
    assert treated.change("mean_headway_minutes").delta < 0
    assert treated.change("mean_headway_minutes").significant
    assert not control.change("mean_headway_minutes").significant
    net = {row.metric: row for row in did}
    assert net["mean_headway_minutes"].estimate == pytest.approx(
        treated.change("mean_headway_minutes").delta - control.change("mean_headway_minutes").delta
    )
    assert net["mean_headway_minutes"].significant

    strict = ImpactParameters(min_days=10, bootstrap_samples=200)
    impacts, did, skipped = analyze(series, event, start, end, strict, lambda _: "treated")
    assert impacts == [] and did == [] and len(skipped) == 2
    assert "días con datos" in skipped[0].reason

    with pytest.raises(ValueError):
        analyze(series, end + timedelta(days=1), start, end, parameters, lambda _: "treated")


def test_clip_windows_never_extends_past_now() -> None:
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    event = datetime(2026, 10, 10, tzinfo=timezone.utc)
    start, end = clip_windows(event, 7, 7, now)
    assert start == event - timedelta(days=7) and end == now
    with pytest.raises(ValueError):
        clip_windows(now + timedelta(hours=1), 7, 7, now)
    with pytest.raises(ValueError):
        clip_windows(event, 0, 7, now)


def test_demo_cli_writes_report_and_refuses_existing_directory(tmp_path: Path) -> None:
    output = tmp_path / "demo"
    argv = ["demo", "--days", "6", "--seed", "3", "--bootstrap-samples", "200"]
    assert main([*argv, "--output", str(output)]) == 0
    summary = ImpactSummary.load(output / "summary.json")
    assert summary.source == "synthetic" and len(summary.treated) == 1
    assert summary.start < summary.event < summary.end_exclusive
    assert {row.metric for row in summary.difference_in_differences} == {
        "mean_headway_minutes",
        "expected_wait_minutes",
        "saturation_rate",
        "episodes_per_day",
    }
    with (output / "changes.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["role"] for row in rows} == {"treated", "control"}
    report = (output / "report.html").read_text(encoding="utf-8")
    assert "DEMO SINTÉTICA" in report and "<!--BUNCHING_CONTENT-->" not in report
    assert "Diferencias en diferencias" in report and "no implica causalidad" in report
    assert "https://" not in report and "<script src" not in report
    assert main([*argv, "--output", str(output)]) == 2
    assert main(["demo", "--days", "2", "--output", str(tmp_path / "short")]) == 2


def test_cli_empty_database_reports_no_routes(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr("emt_collector.impact.cli.make_engine", lambda _: engine)
    output = tmp_path / "report"
    event = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert main(["analyze", "--event", event, "--output", str(output)]) == 3
    summary = json.loads((output / "summary.json").read_text())
    assert summary["routes"] == [] and summary["source"] == "database"
    report = (output / "report.html").read_text()
    assert "HISTÓRICO DE LA BASE DE DATOS" in report
    assert "Ninguna ruta" in report
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert main(["analyze", "--event", future, "--output", str(tmp_path / "x")]) == 2
