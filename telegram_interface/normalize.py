"""Inbound Telegram payload normalization."""

from __future__ import annotations

import re
from typing import Any

from telegram_interface.models import NormalizedTelegramUpdate, TelegramAttachment

_CMD = re.compile(r"^/([a-zA-Z0-9_]+)(?:@\w+)?(?:\s+(.*))?$")
_ALLOWED_DOC_EXT = frozenset({".xlsx", ".xls", ".csv", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".txt"})


def _ext(name: str) -> str:
    from pathlib import Path

    return Path(name or "").suffix.lower()


def normalize_telegram_payload(payload: dict[str, Any]) -> NormalizedTelegramUpdate:
    if "callback_query" in payload:
        cb = payload["callback_query"] or {}
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        user = cb.get("from") or {}
        return NormalizedTelegramUpdate(
            update_id=str(payload.get("update_id")),
            kind="callback_query",
            chat_id=str(chat.get("id") or ""),
            telegram_user_id=str(user.get("id") or ""),
            callback_data=str(cb.get("data") or ""),
            callback_query_id=str(cb.get("id") or ""),
            raw_kind="callback_query",
        )

    message = payload.get("message") or payload.get("edited_message") or {}
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    text = str(message.get("text") or message.get("caption") or "")
    chat_id = str(chat.get("id") or "")
    user_id = str(user.get("id") or "")
    update_id = str(payload.get("update_id"))

    attachment = None
    doc = message.get("document")
    if doc:
        fname = str(doc.get("file_name") or "upload.bin")
        attachment = TelegramAttachment(
            file_id=str(doc.get("file_id") or ""),
            filename=fname,
            mime_type=str(doc.get("mime_type") or "application/octet-stream"),
            size_bytes=int(doc.get("file_size") or 0),
            kind="document",
        )
    elif message.get("photo"):
        photos = message.get("photo") or []
        best = photos[-1] if photos else {}
        attachment = TelegramAttachment(
            file_id=str(best.get("file_id") or ""),
            filename="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=int(best.get("file_size") or 0),
            kind="photo",
        )

    if text.startswith("/"):
        m = _CMD.match(text.strip())
        if m:
            return NormalizedTelegramUpdate(
                update_id=update_id,
                kind="command",
                chat_id=chat_id,
                telegram_user_id=user_id,
                text=text,
                command=m.group(1).lower(),
                command_args=(m.group(2) or "").strip(),
                attachment=attachment,
                raw_kind="command",
            )

    return NormalizedTelegramUpdate(
        update_id=update_id,
        kind="message",
        chat_id=chat_id,
        telegram_user_id=user_id,
        text=text,
        attachment=attachment,
        raw_kind="message",
    )


def attachment_allowed(attachment: TelegramAttachment) -> bool:
    if attachment.size_bytes > 10 * 1024 * 1024:
        return False
    ext = _ext(attachment.filename)
    if attachment.kind == "photo":
        return ext in {".jpg", ".jpeg", ".png", ".webp"} or ext == ""
    return ext in _ALLOWED_DOC_EXT
