from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from emt_collector.analysis.cli import main
from emt_collector.db.repository import init_schema


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'history.db'}"
    engine = create_engine(url, future=True)
    init_schema(engine, use_timescale=False)
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)


def test_run_executes_the_three_analyses_and_writes_summaries(
    database: None, tmp_path: Path
) -> None:
    output = tmp_path / "reports"
    assert main(["run", "--output", str(output), "--days", "7", "--stop", "100"]) == 3
    latest = json.loads((output / "latest.json").read_text())
    folder = Path(latest["folder"])
    assert folder.parent == output
    assert json.loads((folder / "summary.json").read_text()) == latest
    assert [r["analysis"] for r in latest["results"]] == ["bunching", "saturation", "frequency"]
    assert {r["status"] for r in latest["results"]} == {"insufficient_data"}
    for result in latest["results"]:
        assert (Path(result["output"]) / "report.html").exists()
    assert latest["stops"] == ["100"]


def test_only_limits_the_analyses(database: None, tmp_path: Path) -> None:
    output = tmp_path / "reports"
    main(["run", "--output", str(output), "--only", "frequency"])
    latest = json.loads((output / "latest.json").read_text())
    assert [r["analysis"] for r in latest["results"]] == ["frequency"]
