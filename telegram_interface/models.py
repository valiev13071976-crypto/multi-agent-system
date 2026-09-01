"""Normalized Telegram contracts — no raw provider payloads in domain layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TelegramAttachment:
    file_id: str
    filename: str
    mime_type: str
    size_bytes: int
    kind: str  # document | photo


@dataclass(frozen=True)
class NormalizedTelegramUpdate:
    update_id: str
    kind: str  # message | callback_query | command
    chat_id: str
    telegram_user_id: str
    text: str = ""
    command: str = ""
    command_args: str = ""
    callback_data: str = ""
    callback_query_id: str = ""
    attachment: TelegramAttachment | None = None
    raw_kind: str = ""


@dataclass
class TelegramBinding:
    binding_id: str
    tenant_id: str
    owner_id: str
    telegram_user_id: str
    chat_id: str
    conversation_id: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class CallbackToken:
    token: str
    tenant_id: str
    owner_id: str
    request_id: str
    action: str  # approve | reject | cancel
    approval_id: str = ""
    created_at: str = ""
    consumed: bool = False


@dataclass
class ChatSession:
    chat_id: str
    tenant_id: str
    owner_id: str
    conversation_id: str
    active_request_id: str = ""
    progress_message_id: str = ""
    last_event_cursor: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
