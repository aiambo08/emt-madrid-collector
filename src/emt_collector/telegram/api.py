from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

TELEGRAM_BASE_URL = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096


class TelegramError(Exception):
    """The Bot API rejected a call or could not be reached."""


class Chat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    type: str = "private"


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: int
    chat: Chat
    text: str | None = None


class Update(BaseModel):
    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: Message | None = None
    edited_message: Message | None = None

    @property
    def text_message(self) -> Message | None:
        message = self.message or self.edited_message
        return message if message and message.text else None


class BotCommand(BaseModel):
    command: str
    description: str = Field(max_length=256)


class TelegramAPI:
    """Cliente mínimo y sincrónico de la Bot API (long polling, sin dependencias extra)."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = TELEGRAM_BASE_URL,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("token vacío")
        self._http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/bot{token}", timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TelegramAPI:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        try:
            response = self._http.post(f"/{method}", json=params, timeout=timeout)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}: {exc}") from exc
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description") if isinstance(body, dict) else body
            raise TelegramError(f"{method}: HTTP {response.status_code}: {description}")
        return body.get("result")

    def get_me(self) -> dict[str, Any]:
        result = self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    def get_updates(self, offset: int | None, timeout_seconds: int) -> list[Update]:
        params: dict[str, Any] = {"timeout": timeout_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", params, timeout=timeout_seconds + 10)
        return [Update.model_validate(item) for item in result or []]

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in _chunks(text, MAX_MESSAGE_CHARS):
            self._call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )

    def set_my_commands(self, commands: list[BotCommand]) -> None:
        self._call("setMyCommands", {"commands": [c.model_dump() for c in commands]})


def _chunks(text: str, size: int) -> list[str]:
    lines = text.split("\n")
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > size and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
        while len(current) > size:
            chunks.append(current[:size])
            current = current[size:]
    chunks.append(current)
    return chunks
