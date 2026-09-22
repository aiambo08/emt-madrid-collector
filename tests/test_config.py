from __future__ import annotations

import pytest
from pydantic import ValidationError

from emt_collector.config import Settings


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "EMT_EMAIL",
        "EMT_PASSWORD",
        "EMT_CLIENT_ID",
        "EMT_PASS_KEY",
        "EMT_LINES",
        "EMT_STOPS",
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


def test_requires_credentials_and_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    with pytest.raises(ValidationError, match="EMT_EMAIL"):
        Settings(_env_file=None, emt_lines="27")
    with pytest.raises(ValidationError, match="EMT_LINES"):
        Settings(_env_file=None, emt_client_id="c", emt_pass_key="k")


def test_app_credentials_alone_are_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    s = Settings(_env_file=None, emt_client_id="c", emt_pass_key="k", emt_stops="1")
    assert s.emt_email is None
    assert s.cycles_per_day == 1440
