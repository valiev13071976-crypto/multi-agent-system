"""Telegram provider contract and inbound normalization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class TelegramSendRequest:
    tenant_id: str
    chat_id: str
    text: str
    idempotency_key: str


@dataclass
class TelegramSendResult:
    provider_message_id: str
    status: str
    chat_id: str


@dataclass
class TelegramInboundUpdate:
    update_id: str
    bot_id: str
    chat_id: str
    text: str
    attachment_ref: str = ""
    ordering_key: str = ""


class TelegramProvider(Protocol):
    def send_message(self, request: TelegramSendRequest) -> TelegramSendResult: ...


def normalize_inbound(raw: dict[str, Any]) -> TelegramInboundUpdate:
    return TelegramInboundUpdate(
        update_id=str(raw.get("update_id") or raw.get("id") or uuid.uuid4().hex),
        bot_id=str(raw.get("bot_id") or raw.get("bot") or ""),
        chat_id=str(raw.get("chat_id") or raw.get("chat") or ""),
        text=str(raw.get("text") or raw.get("message") or ""),
        attachment_ref=str(raw.get("attachment_ref") or raw.get("document_id") or ""),
        ordering_key=str(raw.get("ordering_key") or raw.get("update_id") or ""),
    )
