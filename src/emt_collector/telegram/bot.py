from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy.exc import SQLAlchemyError

from emt_collector.telegram.api import TelegramAPI, TelegramError
from emt_collector.telegram.data import DataSource
from emt_collector.telegram.handlers import COMMANDS, Alerter, Handlers

log = structlog.get_logger(__name__)

POLL_TIMEOUT_SECONDS = 25
ERROR_BACKOFF_SECONDS = 5


class Bot:
    """Long polling + alertas periódicas. Sin hilos: una iteración = un `getUpdates`."""

    def __init__(
        self,
        api: TelegramAPI,
        source: DataSource,
        *,
        expected_interval_seconds: int,
        allowed_chats: set[int],
        alert_chats: list[int],
        alert_every: timedelta,
        alerter: Alerter,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api = api
        self._source = source
        self._handlers = Handlers(source, expected_interval_seconds)
        self._allowed = allowed_chats
        self._alert_chats = alert_chats
        self._alert_every = alert_every
        self._alerter = alerter
        self._now = now
        self._sleep = sleep
        self._offset: int | None = None
        self._next_alert = now()
        self.stopping = False

    def stop(self) -> None:
        self.stopping = True

    def setup(self) -> None:
        me = self._api.get_me()
        self._api.set_my_commands(COMMANDS)
        log.info("bot.ready", username=me.get("username"), alert_chats=len(self._alert_chats))

    def run(self) -> None:
        self.setup()
        while not self.stopping:
            try:
                self.step()
            except TelegramError as exc:
                log.warning("bot.telegram_error", error=str(exc))
                self._sleep(ERROR_BACKOFF_SECONDS)

    def step(self, poll_timeout: int = POLL_TIMEOUT_SECONDS) -> None:
        self.alerts()
        for update in self._api.get_updates(self._offset, poll_timeout):
            self._offset = update.update_id + 1
            message = update.text_message
            if message is None or message.text is None:
                continue
            if self._allowed and message.chat.id not in self._allowed:
                log.info("bot.chat_rejected", chat_id=message.chat.id)
                continue
            self.reply(message.chat.id, message.text)

    def reply(self, chat_id: int, text: str) -> None:
        try:
            answer = self._handlers.handle(text, self._now())
        except SQLAlchemyError as exc:
            log.error("bot.db_error", error=str(exc))
            answer = "No se pudo consultar la base de datos; inténtalo más tarde."
        except ValueError as exc:
            log.warning("bot.handler_error", error=str(exc))
            answer = f"No se pudo atender la petición: {exc}"
        if answer:
            self._api.send_message(chat_id, answer)
            log.info("bot.replied", chat_id=chat_id, command=text.split()[0][:32])

    def alerts(self) -> None:
        now = self._now()
        if not self._alert_chats or now < self._next_alert:
            return
        self._next_alert = now + self._alert_every
        try:
            risks = self._source.risks(None, now)
        except (SQLAlchemyError, ValueError) as exc:
            log.warning("bot.alerts_failed", error=str(exc))
            return
        names = {r.route.stop_id: self._source.stop_name(r.route.stop_id) for r in risks}
        for text in self._alerter.messages(risks, now, names):
            for chat_id in self._alert_chats:
                try:
                    self._api.send_message(chat_id, text)
                except TelegramError as exc:
                    log.warning("bot.alert_failed", chat_id=chat_id, error=str(exc))
            log.info("bot.alert_sent", chats=len(self._alert_chats))
