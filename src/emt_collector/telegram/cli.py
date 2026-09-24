from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType

import structlog
from pydantic import ValidationError

from emt_collector.__main__ import build_client
from emt_collector.config import ConfigError, Settings
from emt_collector.db.repository import make_engine
from emt_collector.logging_setup import configure_logging
from emt_collector.telegram.api import TelegramAPI, TelegramError
from emt_collector.telegram.bot import Bot
from emt_collector.telegram.data import LiveDataSource
from emt_collector.telegram.handlers import Alerter
from emt_collector.telegram.models import ModelStore

log = structlog.get_logger(__name__)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="emt-bot",
        description="Bot de Telegram: llegadas EMT en tiempo real, riesgo previsto y estado.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="atender comandos y enviar alertas hasta SIGTERM")
    run.add_argument(
        "--reports",
        type=Path,
        default=None,
        help="carpeta de emt-analysis con latest.json (por defecto REPORTS_DIR)",
    )
    commands.add_parser("check", help="validar token, BD y modelos sin atender mensajes")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = Settings()
        settings.require_telegram()
        settings.require_credentials()
        database_url = settings.resolved_database_url()
    except (ValidationError, ConfigError) as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings.log_format)
    assert settings.telegram_bot_token
    reports = args.reports if args.command == "run" and args.reports else settings.reports_dir
    store = ModelStore(reports)
    engine = make_engine(database_url)
    try:
        with (
            TelegramAPI(settings.telegram_bot_token) as api,
            build_client(settings) as client,
        ):
            source = LiveDataSource(client, engine, store)
            bot = Bot(
                api,
                source,
                expected_interval_seconds=settings.collect_interval_seconds,
                allowed_chats=settings.telegram_allowed_chats,
                alert_chats=settings.telegram_alert_chats,
                alert_every=timedelta(minutes=settings.telegram_alert_every_minutes),
                alerter=Alerter(
                    settings.telegram_alert_probability,
                    timedelta(minutes=settings.telegram_alert_cooldown_minutes),
                ),
            )
            if args.command == "check":
                return _check(api, source, reports)
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, _stopper(bot))
            bot.run()
    except TelegramError as exc:
        print(f"Telegram error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


def _check(api: TelegramAPI, source: LiveDataSource, reports: Path) -> int:
    me = api.get_me()
    loaded = source.models()
    status = source.status(datetime.now(timezone.utc))
    print(
        f"bot: @{me.get('username')}\n"
        f"reports: {reports} (latest.json {'presente' if loaded.generated_at else 'ausente'})\n"
        f"modelo bunching: {'sí' if loaded.bunching else 'no'}\n"
        f"modelo saturación: {'sí' if loaded.saturation else 'no'}\n"
        f"último ciclo: {status.last_cycle.started_at.isoformat() if status.last_cycle else '—'}"
    )
    return 0


def _stopper(bot: Bot) -> Callable[[int, FrameType | None], None]:
    def _handler(signum: int, _frame: FrameType | None) -> None:
        log.info("bot.stopping", signal=signal.Signals(signum).name)
        bot.stop()

    return _handler


if __name__ == "__main__":
    sys.exit(main())
