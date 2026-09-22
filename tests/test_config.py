from __future__ import annotations

import pytest
from pydantic import ValidationError

from emt_collector.__main__ import main
from emt_collector.config import ConfigError, Settings


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "EMT_EMAIL",
        "EMT_PASSWORD",
        "EMT_CLIENT_ID",
        "EMT_PASS_KEY",
        "EMT_LINES",
        "EMT_STOPS",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_csv_lists_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("EMT_EMAIL", "u@example.com")
    monkeypatch.setenv("EMT_PASSWORD", "p")
    monkeypatch.setenv("EMT_LINES", "27")
    monkeypatch.setenv("EMT_STOPS", " 62, 63 ,")
    s = Settings(_env_file=None)
    assert s.emt_lines == ["27"]
    assert s.emt_stops == ["62", "63"]


def test_command_specific_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    bare = Settings(_env_file=None)
    with pytest.raises(ConfigError, match="EMT_EMAIL"):
        bare.require_credentials()
    with pytest.raises(ConfigError, match="EMT_LINES"):
        bare.require_targets()
    with_creds = Settings(_env_file=None, emt_client_id="c", emt_pass_key="k")
    with_creds.require_credentials()
    with pytest.raises(ConfigError, match="EMT_LINES"):
        with_creds.require_targets()
    Settings(_env_file=None, emt_client_id="c", emt_pass_key="k", emt_stops="1").require_targets()


def test_interval_lower_bound() -> None:
    with pytest.raises(ValidationError, match="COLLECT_INTERVAL_SECONDS"):
        Settings(_env_file=None, collect_interval_seconds=1)


def test_init_db_needs_no_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.chdir("/")  # no .env
    assert main(["init-db"]) == 0
    assert main(["once"]) == 2
    assert "EMT_EMAIL" in capsys.readouterr().err
