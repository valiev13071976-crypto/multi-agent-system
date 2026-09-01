"""Spoken approval intent detection — explicit phrases only."""

from __future__ import annotations

_APPROVAL_PHRASES = (
    "approve",
    "yes approve",
    "confirm approval",
    "подтверждаю",
    "да, подтверждаю",
    "да подтверждаю",
    "одобряю",
    "подтвердить",
)


def normalize_transcript(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def is_explicit_approval_intent(transcript: str) -> bool:
    t = normalize_transcript(transcript).casefold()
    if not t:
        return False
    for phrase in _APPROVAL_PHRASES:
        if t == phrase.casefold() or t.startswith(phrase.casefold() + " "):
            return True
    return False


def is_reject_intent(transcript: str) -> bool:
    t = normalize_transcript(transcript).casefold()
    return t in {"reject", "отклонить", "отклоняю", "no", "нет"}


def is_cancel_intent(transcript: str) -> bool:
    t = normalize_transcript(transcript).casefold()
    return t in {"cancel", "отменить", "отмена", "stop"}
