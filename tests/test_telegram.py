from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from emt_collector.api.client import EMTClient, EMTError
from emt_collector.bunching.demo import synthetic_history
from emt_collector.bunching.detector import build_series, detect
from emt_collector.bunching.domain import Parameters, Route
from emt_collector.bunching.features import examples
from emt_collector.bunching.model import ForecastModel, train_model
from emt_collector.config import Settings
from emt_collector.db.models import ArrivalEstimate, CollectionCycle, CollectionGap, Stop
from emt_collector.telegram.api import MAX_MESSAGE_CHARS, TelegramAPI, TelegramError, _chunks
from emt_collector.telegram.bot import Bot
from emt_collector.telegram.cli import main
from emt_collector.telegram.data import Arrival, LiveDataSource, Risk, Status
from emt_collector.telegram.handlers import Alerter, Handlers, format_status
from emt_collector.telegram.models import EMPTY, Loaded, ModelStore
from tests.conftest import BASE_URL, LOGIN_OK, arrivals_response, arrive

NOW = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)
ROUTE = Route("27", "1182", "PLAZA CASTILLA")


# --- Telegram API ---------------------------------------------------------------------------


class FakeTelegram:
    """Registra las llamadas al Bot API y sirve `getUpdates` desde una cola."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[list[dict[str, Any]]] = []
        self.fail_next: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content or b"{}")
        self.calls.append((method, params))
        assert "bot123:secret" in request.url.path
        if self.fail_next == method:
            self.fail_next = None
            return httpx.Response(502, text="<html>bad gateway</html>")
        if method == "getUpdates":
            batch = self.updates.pop(0) if self.updates else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "emt_test_bot"}})
        if method == "sendMessage" and params["chat_id"] == 666:
            return httpx.Response(
                403, json={"ok": False, "description": "Forbidden: bot was blocked"}
            )
        return httpx.Response(200, json={"ok": True, "result": True})

    def api(self) -> TelegramAPI:
        return TelegramAPI("123:secret", transport=httpx.MockTransport(self.handler))

    def sent(self, chat_id: int | None = None) -> list[str]:
        return [
            str(p["text"])
            for m, p in self.calls
            if m == "sendMessage" and (chat_id is None or p["chat_id"] == chat_id)
        ]


def update(update_id: int, chat_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def test_api_splits_long_messages_and_surfaces_errors() -> None:
    fake = FakeTelegram()
    with fake.api() as api:
        long_text = "\n".join(f"línea {i} " + "x" * 100 for i in range(60))
        api.send_message(1, long_text)
        chunks = fake.sent(1)
        assert len(chunks) == 2 and all(len(c) <= MAX_MESSAGE_CHARS for c in chunks)
        assert "".join(chunks).replace("\n", "") == long_text.replace("\n", "")
        with pytest.raises(TelegramError, match="Forbidden"):
            api.send_message(666, "hola")
        fake.fail_next = "getUpdates"
        with pytest.raises(TelegramError, match="getUpdates"):
            api.get_updates(None, 0)
    with pytest.raises(ValueError):
        TelegramAPI("")
    assert list(_chunks("a" * 10, 4)) == ["aaaa", "aaaa", "aa"]


# --- Model store ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bunching_model() -> ForecastModel:
    observations, gaps = synthetic_history(days=14)
    rows = []
    for series in build_series(observations, gaps, Parameters()):
        rows.extend(examples(series, detect(series), series.times[-1]))
    model, _ = train_model(rows, Parameters(), "synthetic")
    return model


def write_reports(
    reports: Path,
    bunching: ForecastModel | None,
    absolute: bool = True,
    folder: str = "20261001T0800Z",
) -> Path:
    run = reports / folder
    (run / "bunching").mkdir(parents=True)
    (run / "saturation").mkdir()
    if bunching is not None:
        bunching.save(run / "bunching" / "model.json")
    output = str(run / "bunching") if absolute else f"{folder}/bunching"
    summary = {
        "generated_at": "2026-10-01T08:00:00+00:00",
        "results": [
            {"analysis": "bunching", "exit_code": 0, "status": "ok", "output": output},
            {"analysis": "saturation", "exit_code": 4, "status": "error", "output": str(run / "s")},
            {"analysis": "frequency", "exit_code": 0, "status": "ok", "output": str(run / "f")},
        ],
    }
    (reports / "latest.json").write_text(json.dumps(summary), encoding="utf-8")
    return reports / "latest.json"


def test_model_store_ignores_synthetic_and_reloads_when_latest_changes(
    tmp_path: Path, bunching_model: ForecastModel
) -> None:
    store = ModelStore(tmp_path / "missing")
    assert store.current is EMPTY

    reports = tmp_path / "reports"
    latest = write_reports(reports, bunching_model)
    store = ModelStore(reports)
    assert store.current.bunching is None  # sintético: nunca se sirve como predicción real
    assert store.current.generated_at == datetime(2026, 10, 1, 8, tzinfo=timezone.utc)

    real = bunching_model.model_copy(update={"source": "database"})
    real.save(reports / "20261001T0800Z" / "bunching" / "model.json")
    assert store.current.bunching is None  # latest.json no cambió: caché
    latest.write_text(latest.read_text() + " ", encoding="utf-8")
    loaded = store.current
    assert loaded.bunching is not None and loaded.saturation is None
    assert loaded.stops == sorted({r[1] for r in real.routes})

    latest.write_text("{not json", encoding="utf-8")
    assert store.current is EMPTY
    latest.write_text(json.dumps({"results": "nope", "generated_at": "ayer"}), encoding="utf-8")
    assert store.current is EMPTY


def test_model_store_resolves_container_paths_relative_to_reports(
    tmp_path: Path, bunching_model: ForecastModel
) -> None:
    reports = tmp_path / "reports"
    real = bunching_model.model_copy(update={"source": "database"})
    write_reports(reports, real, absolute=False)
    assert ModelStore(reports).current.bunching is not None
    # Rutas absolutas de otro host (p. ej. /reports/... escritas dentro de Docker)
    summary = json.loads((reports / "latest.json").read_text())
    summary["results"][0]["output"] = "/reports/20261001T0800Z/bunching"
    (reports / "latest.json").write_text(json.dumps(summary), encoding="utf-8")
    assert ModelStore(reports).current.bunching is not None


# --- Data source ------------------------------------------------------------------------------


def emt_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/login/"):
        return httpx.Response(200, json=LOGIN_OK)
    if "/stops/9999/" in request.url.path:
        return httpx.Response(200, json={"code": "90", "description": "no stop", "data": []})
    assert request.url.path.endswith("/stops/1182/arrives/45/") or request.url.path.endswith(
        "/stops/1182/arrives/"
    )
    return httpx.Response(
        200,
        json=arrivals_response(
            [
                arrive("45", "1182", 4321, eta=600),
                arrive("027", "1182", 1111, eta=30),
                arrive("27", "1182", 2222, eta=999999),
            ]
        ),
    )


@pytest.fixture
def emt_client() -> EMTClient:
    return EMTClient(
        BASE_URL,
        email="u@example.com",
        password="p",
        transport=httpx.MockTransport(emt_handler),
        clock=lambda: 1000.0,
        sleep=lambda _s: None,
    )


def seed_status(engine: Engine, now: datetime) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            Stop(stop_id="1182", name="Cibeles", lat=40.4, lon=-3.7, lines="27,45", updated_at=now)
        )
        for i in range(3):
            started = now - timedelta(minutes=2 + i)
            session.add(
                CollectionCycle(
                    started_at=started,
                    finished_at=started + timedelta(seconds=3),
                    status="ok" if i else "partial",
                    stops_requested=4,
                    stops_ok=4 - (i == 0),
                    stops_failed=int(i == 0),
                    arrivals_inserted=50,
                )
            )
        session.add(CollectionCycle(started_at=now, status="running"))
        session.add(
            CollectionGap(occurred_at=now - timedelta(minutes=2), scope="stop", kind="network")
        )
        session.add(
            CollectionGap(occurred_at=now - timedelta(hours=3), scope="cycle", kind="failed")
        )


def test_live_source_arrivals_status_and_stop_name(
    engine: Engine, emt_client: EMTClient, tmp_path: Path
) -> None:
    seed_status(engine, NOW)
    source = LiveDataSource(emt_client, engine, ModelStore(tmp_path))
    rows = source.arrivals("1182", None)
    assert [(r.line, r.eta_seconds) for r in rows] == [("27", 30), ("45", 600), ("27", None)]
    assert source.arrivals("1182", "45")[0].bus_id == 4321
    with pytest.raises(EMTError):
        source.arrivals("9999", None)
    assert source.stop_name("1182") == "Cibeles" and source.stop_name("1") is None

    status = source.status(NOW)
    assert status.last_cycle is not None and status.last_cycle.status == "partial"
    assert status.last_cycle.stops_failed == 1
    assert (status.cycles_last_hour, status.gaps_last_hour, status.arrivals_last_hour) == (
        4,
        1,
        150,
    )
    assert source.risks(["1182"], NOW) == []


def test_live_source_risks_use_trained_model_on_recent_history(
    engine: Engine, emt_client: EMTClient, tmp_path: Path, bunching_model: ForecastModel
) -> None:
    observations, _ = synthetic_history(days=14)
    end = max(o.sample_ts for o in observations)
    recent = [o for o in observations if o.sample_ts >= end - timedelta(hours=3)]
    with Session(engine) as session, session.begin():
        for o in recent:
            session.add(
                ArrivalEstimate(
                    stop_id=o.route.stop_id,
                    line=o.route.line,
                    bus_id=o.bus_id,
                    sample_ts=o.sample_ts,
                    ingested_at=o.ingested_at,
                    estimate_seconds=o.eta if o.eta is not None else 999999,
                    distance_m=o.distance_m,
                    destination=o.route.destination,
                    is_head=o.is_head,
                )
            )
    write_reports(tmp_path, bunching_model.model_copy(update={"source": "database"}))
    source = LiveDataSource(emt_client, engine, ModelStore(tmp_path))
    at = end + timedelta(minutes=1)
    risks = source.risks(None, at)
    assert {r.route for r in risks} == {Route(*r) for r in bunching_model.routes}
    assert all(r.saturation_status is None for r in risks)
    ok = [r for r in risks if r.bunching_status == "ok"]
    assert ok and all(0 <= (r.bunching_probability or 0) <= 1 for r in ok)
    assert ok[0].horizon_minutes == 15
    stop = ok[0].route.stop_id
    assert {r.route.stop_id for r in source.risks([stop], at)} == {stop}


# --- Handlers / bot (fuente simulada) ----------------------------------------------------------


class FakeSource:
    def __init__(self) -> None:
        self.risk_rows: list[Risk] = []
        self.arrival_calls = 0

    def arrivals(self, stop: str, line: str | None) -> list[Arrival]:
        self.arrival_calls += 1
        if stop == "9999":
            raise EMTError("EMT API code 90: no stop")
        rows = [
            Arrival("27", "PLAZA CASTILLA", 1, 30, 120),
            Arrival("45", "REINA VICTORIA", 2, 600, None),
        ]
        rows.append(Arrival("27", "PLAZA CASTILLA", 3, None, None))
        return [r for r in rows if line is None or r.line == line]

    def stop_name(self, stop: str) -> str | None:
        return "Cibeles <b>" if stop == "1182" else None

    def risks(self, stops: list[str] | None, at: datetime) -> list[Risk]:
        return [r for r in self.risk_rows if stops is None or r.route.stop_id in stops]

    def status(self, at: datetime) -> Status:
        return Status(at, None, 0, 0, 0, EMPTY)

    def models(self) -> Loaded:
        return EMPTY


def risk(
    line: str = "27",
    stop: str = "1182",
    bunching: float | None = 0.8,
    saturation: float | None = 0.2,
) -> Risk:
    return Risk(
        Route(line, stop, "PLAZA CASTILLA"),
        bunching,
        "ok" if bunching is not None else "insufficient_coverage",
        15,
        saturation,
        "ok" if saturation is not None else "insufficient_coverage",
        11.0 if saturation is not None else None,
        9.0,
        12.0,
    )


def test_handlers_format_commands_and_validate_arguments() -> None:
    source = FakeSource()
    handlers = Handlers(source, 60)
    assert handlers.handle("hola", NOW) is None
    assert "/llegadas" in (handlers.handle("/start", NOW) or "")
    assert "Uso: /llegadas" in (handlers.handle("/llegadas", NOW) or "")
    assert "Uso: /llegadas" in (handlers.handle("/llegadas abc", NOW) or "")
    assert "desconocido" in (handlers.handle("/foo", NOW) or "")

    text = handlers.handle("/llegadas@emt_bot 1182", NOW) or ""
    assert "Cibeles &lt;b&gt;" in text and "<b>27</b> → PLAZA CASTILLA: llegando · 120 m" in text
    assert "45</b> → REINA VICTORIA: 10 min" in text and "sin estimación" in text
    only = handlers.handle("/llegadas 1182 045", NOW) or ""
    assert "línea 45" in only and "REINA" in only and "PLAZA" not in only
    assert "no respondió" in (handlers.handle("/llegadas 9999", NOW) or "")

    assert "Ningún modelo cubre" in (handlers.handle("/riesgo 1182", NOW) or "")
    source.risk_rows = [risk(), risk(line="45", bunching=None, saturation=None)]
    text = handlers.handle("/riesgo 1182", NOW) or ""
    assert "bunching 80 % en 15 min" in text and "intervalo saturado 20 %" in text
    assert "espera prevista 11 min" in text and "último bus hace 9 min, umbral 12 min" in text
    assert "<b>45</b> → PLAZA CASTILLA: sin muestras recientes suficientes." in text
    assert "y línea" in (handlers.handle("/riesgo 1182 99", NOW) or "")

    text = handlers.handle("/estado", NOW) or ""
    assert "Sin ciclos registrados" in text and "ninguno cargado" in text
    assert source.arrival_calls == 3  # /estado y /riesgo no llaman a la API EMT


def test_format_status_flags_stale_or_failed_cycles(
    engine: Engine, emt_client: EMTClient, tmp_path: Path
) -> None:
    seed_status(engine, NOW)
    source = LiveDataSource(emt_client, engine, ModelStore(tmp_path))
    text = format_status(source.status(NOW), 60)
    assert "⚠️ Último ciclo" in text and "partial, 3 paradas OK, 1 fallidas, 50 llegadas" in text
    assert "4 ciclos de 60 esperados, 150 llegadas, 1 gaps" in text
    late = format_status(source.status(NOW + timedelta(hours=1)), 60)
    assert "⚠️" in late and "(62 min)" in late


def test_alerter_thresholds_and_cooldown() -> None:
    alerter = Alerter(0.6, timedelta(minutes=30))
    rows = [risk(bunching=0.7, saturation=0.65), risk(line="45", bunching=0.3, saturation=None)]
    names = {"1182": "Cibeles"}
    first = alerter.messages(rows, NOW, names)
    assert len(first) == 2 and "bunching 70 %" in first[0] and "saturado probable 65 %" in first[1]
    assert "parada 1182 (Cibeles)" in first[0]
    assert alerter.messages(rows, NOW + timedelta(minutes=10), names) == []
    assert len(alerter.messages(rows, NOW + timedelta(minutes=31), names)) == 2


def make_bot(fake: FakeTelegram, source: FakeSource, **kwargs: Any) -> tuple[Bot, list[float]]:
    slept: list[float] = []
    options: dict[str, Any] = {
        "expected_interval_seconds": 60,
        "allowed_chats": set(),
        "alert_chats": [],
        "alert_every": timedelta(minutes=5),
        "alerter": Alerter(0.6, timedelta(minutes=30)),
        "now": lambda: NOW,
        "sleep": slept.append,
    }
    options.update(kwargs)
    return Bot(fake.api(), source, **options), slept


def test_bot_polls_replies_filters_chats_and_sends_alerts() -> None:
    fake = FakeTelegram()
    source = FakeSource()
    source.risk_rows = [risk(bunching=0.9)]
    fake.updates = [
        [update(1, 10, "/estado"), update(2, 20, "/llegadas 1182"), update(3, 10, "texto libre")],
        [update(4, 10, "/riesgo 1182")],
    ]
    bot, _ = make_bot(fake, source, allowed_chats={10, 30}, alert_chats=[30])
    bot.setup()
    assert [m for m, _ in fake.calls[:2]] == ["getMe", "setMyCommands"]
    bot.step(poll_timeout=0)
    assert fake.sent(20) == []  # chat no autorizado
    assert len(fake.sent(10)) == 1 and "Estado del recolector" in fake.sent(10)[0]
    assert "bunching 90 %" in fake.sent(30)[0]  # alerta en el primer paso
    bot.step(poll_timeout=0)
    assert "Riesgo en parada 1182" in fake.sent(10)[1]
    assert len(fake.sent(30)) == 1  # sin repetir dentro del intervalo
    offsets = [p.get("offset") for m, p in fake.calls if m == "getUpdates"]
    assert offsets == [None, 4]


def test_bot_run_recovers_from_telegram_errors_and_stops() -> None:
    fake = FakeTelegram()
    bot, slept = make_bot(fake, FakeSource())
    fake.updates = [[update(1, 666, "/ayuda")], [update(2, 7, "/ayuda")]]
    calls = {"n": 0}

    def step(poll_timeout: int = 0) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            bot.stop()
        Bot.step(bot, poll_timeout=0)

    bot.step = step  # type: ignore[method-assign]
    bot.run()
    assert slept == [5]  # el 403 al responder al chat 666 no tumba el bucle
    assert fake.sent(7) == [fake.sent(7)[0]] and "EMT Madrid" in fake.sent(7)[0]


# --- CLI / config -----------------------------------------------------------------------------


def test_settings_parse_and_validate_chat_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    s = Settings(
        _env_file=None, telegram_allowed_chat_ids="1, -100200, 3", telegram_alert_chat_ids=""
    )
    assert s.telegram_allowed_chats == {1, -100200, 3} and s.telegram_alert_chats == []
    with pytest.raises(ValueError, match="chat id inválido"):
        Settings(_env_file=None, telegram_alert_chat_ids="abc")


def test_cli_requires_token_and_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in ("TELEGRAM_BOT_TOKEN", "EMT_EMAIL", "EMT_PASSWORD", "EMT_CLIENT_ID", "EMT_PASS_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.chdir(Path(__file__).parent)  # sin .env
    assert main(["run"]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert main(["check"]) == 2
    assert "EMT_CLIENT_ID" in capsys.readouterr().err
