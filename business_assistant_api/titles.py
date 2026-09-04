"""Deterministic conversation titles — no LLM."""

from __future__ import annotations

DEFAULT_CONVERSATION_TITLE = "Новый чат"
TITLE_MAX_LEN = 80
_DEFAULT_TITLES = frozenset(
    {
        "",
        "new chat",
        "новый чат",
        "conversation",
        "разговор",
    }
)
_TITLE_SOURCE_USER = "user"
_TITLE_SOURCE_AUTO = "auto"
_TITLE_SOURCE_DEFAULT = "default"


def normalize_conversation_title(raw: str, *, max_len: int = TITLE_MAX_LEN) -> str:
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    if len(text) > max_len:
        return text[:max_len].rstrip()
    return text


def is_placeholder_title(title: str, *, title_source: str = "") -> bool:
    source = str(title_source or "").strip().lower()
    if source in {_TITLE_SOURCE_USER, _TITLE_SOURCE_AUTO}:
        return False
    return str(title or "").strip().casefold() in _DEFAULT_TITLES


def derive_auto_title(message: str) -> str:
    return normalize_conversation_title(message)


def title_metadata_for_create(title: str | None) -> dict:
    normalized = normalize_conversation_title(title or "")
    if not normalized:
        return {"title": DEFAULT_CONVERSATION_TITLE, "title_source": _TITLE_SOURCE_DEFAULT}
    if is_placeholder_title(normalized):
        return {"title": DEFAULT_CONVERSATION_TITLE, "title_source": _TITLE_SOURCE_DEFAULT}
    return {"title": normalized, "title_source": _TITLE_SOURCE_USER}


def auto_title_update(metadata: dict | None, message: str) -> dict | None:
    meta = dict(metadata or {})
    source = str(meta.get("title_source") or "")
    current = str(meta.get("title") or "")
    if source == _TITLE_SOURCE_USER:
        return None
    if source == _TITLE_SOURCE_AUTO:
        return None
    if not is_placeholder_title(current, title_source=source):
        return None
    derived = derive_auto_title(message)
    if not derived:
        return None
    return {"title": derived, "title_source": _TITLE_SOURCE_AUTO}


def user_title_update(title: str) -> dict:
    normalized = normalize_conversation_title(title)
    return {"title": normalized, "title_source": _TITLE_SOURCE_USER}
