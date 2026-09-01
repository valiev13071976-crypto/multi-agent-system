"""Outbound Telegram transport — wraps provider without exposing secrets."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from b2b_commerce.telegram import TelegramProvider, TelegramSendRequest, TelegramSendResult


@dataclass
class InlineButton:
    text: str
    callback_data: str


@dataclass
class OutboundMessage:
    chat_id: str
    text: str
    idempotency_key: str
    buttons: list[list[InlineButton]] = field(default_factory=list)
    edit_message_id: str = ""


class TelegramOutboundTransport(Protocol):
    def send(self, message: OutboundMessage) -> TelegramSendResult: ...
    def answer_callback(self, callback_query_id: str, text: str = "") -> None: ...
    def download_file(self, file_id: str) -> tuple[bytes, str]: ...


@dataclass
class ProviderTelegramTransport:
    provider: TelegramProvider
    tenant_id: str
    _file_fixtures: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    callbacks_answered: list[str] = field(default_factory=list)

    def send(self, message: OutboundMessage) -> TelegramSendResult:
        req = TelegramSendRequest(
            tenant_id=self.tenant_id,
            chat_id=message.chat_id,
            text=message.text,
            idempotency_key=message.idempotency_key,
        )
        return self.provider.send_message(req)

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        if callback_query_id:
            self.callbacks_answered.append(callback_query_id)

    def download_file(self, file_id: str) -> tuple[bytes, str]:
        if file_id in self._file_fixtures:
            return self._file_fixtures[file_id]
        return (b"fixture,data\n1,2\n", "fixture.csv")

    def register_file_fixture(self, file_id: str, content: bytes, filename: str) -> None:
        self._file_fixtures[file_id] = (content, filename)


def new_idempotency(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"
