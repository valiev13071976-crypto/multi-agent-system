"""Inbound Telegram payload normalization."""

from __future__ import annotations

import re
from typing import Any

from telegram_interface.models import NormalizedTelegramUpdate, TelegramAttachment

_CMD = re.compile(r"^/([a-zA-Z0-9_]+)(?:@\w+)?(?:\s+(.*))?$")
_ALLOWED_DOC_EXT = frozenset({".xlsx", ".xls", ".csv", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".txt"})
_UNSUPPORTED_KEYS = frozenset(
    {
        "voice",
        "sticker",
        "video",
        "animation",
        "poll",
        "location",
        "contact",
        "venue",
        "dice",
        "audio",
        "video_note",
    }
)
MAX_TELEGRAM_PAYLOAD_BYTES = 65536


def _ext(name: str) -> str:
    from pathlib import Path

    return Path(name or "").suffix.lower()


def normalize_telegram_payload(payload: dict[str, Any]) -> NormalizedTelegramUpdate:
    if not isinstance(payload, dict):
        return NormalizedTelegramUpdate(
            update_id="",
            kind="invalid",
            chat_id="",
            telegram_user_id="",
            raw_kind="invalid",
        )
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

    if "message" not in payload and "edited_message" not in payload:
        return NormalizedTelegramUpdate(
            update_id=str(payload.get("update_id") or ""),
            kind="invalid",
            chat_id="",
            telegram_user_id="",
            raw_kind="invalid",
        )

    message = payload.get("message") or payload.get("edited_message") or {}
    if not isinstance(message, dict) or not message:
        return NormalizedTelegramUpdate(
            update_id=str(payload.get("update_id") or ""),
            kind="invalid",
            chat_id="",
            telegram_user_id="",
            raw_kind="invalid",
        )
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    text = str(message.get("text") or message.get("caption") or "")
    chat_id = str(chat.get("id") or "")
    user_id = str(user.get("id") or "")
    update_id = str(payload.get("update_id"))

    if any(message.get(k) for k in _UNSUPPORTED_KEYS):
        return NormalizedTelegramUpdate(
            update_id=update_id,
            kind="unsupported",
            chat_id=chat_id,
            telegram_user_id=user_id,
            text=text,
            raw_kind="unsupported",
        )

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


def telegram_payload_size_bytes(payload: Any) -> int:
    import json

    try:
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(payload).encode("utf-8"))
