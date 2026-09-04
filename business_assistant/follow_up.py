"""Deterministic conversation follow-up / context resolution.

Does not store history. Does not call models. Bounded prompt injection only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from business_assistant.conversation_gateway import is_internal_assistant_text

KIND_STANDALONE = "standalone"
KIND_NEW_TOPIC = "new_topic"
KIND_TRANSFORM = "transform"
KIND_QUESTION = "question"
KIND_REFERENT = "referent"
KIND_MISSING_CONTEXT = "missing_context"

TARGET_NONE = "none"
TARGET_PREVIOUS_ASSISTANT = "previous_assistant_answer"
TARGET_PREVIOUS_TOPIC = "previous_topic"
TARGET_PREVIOUS_OPTIONS = "previous_options"
TARGET_PREVIOUS_FACT = "previous_fact"

MAX_PRIOR_USER_CHARS = 2_000
MAX_PRIOR_ASSISTANT_CHARS = 12_000

_TRANSFORM_RE = re.compile(
    r"(пометь\s+главн|выдели\s+главн|сделай\s+короче|сократ|"
    r"перепиши|таблиц|списк(?:ом|е)|дай\s+только\s+пункт|"
    r"объясни\s+проще|попроще|сделай\s+проще|"
    r"подробнее|а\s+подробнее|выдели\s+суть|"
    r"одним\s+предложением)",
    re.I,
)
_REFERENT_RE = re.compile(
    r"(а\s+втор|второй\s*\?|перв(?:ый|ая|ое)|последн|"
    r"этот\s+вариант|из\s+них|оба\b|а\s+перв)",
    re.I,
)
_QUESTION_FOLLOW_RE = re.compile(
    r"^(а\s+|и\s+)?(сколько|почему|зачем|откуда|что\s+дальше|"
    r"продолжай|покажи\s+риски|это\s+дорого|какой\s+из|"
    r"а\s+если)",
    re.I,
)
_NEW_TOPIC_RE = re.compile(
    r"(теперь|а\s+теперь|перейд(?:ём|ем)|другая\s+тема|сменим\s+тему)",
    re.I,
)
_NEW_TASK_RE = re.compile(
    r"(сравни|проанализ|разработай|стратег|расскажи\s+про|"
    r"что\s+такое|какие\s+налог|для\s+ип)",
    re.I,
)
_DEICTIC_RE = re.compile(
    r"\b(это|этот|эта|эти|он|она|они|них|того|тем)\b",
    re.I,
)
_MARKETPLACE_RE = re.compile(
    r"(ozon|озон|wildberries|вайлдберриз|\bwb\b|маркетплейс|"
    r"yandex\s+market|яндекс\s+маркет)",
    re.I,
)
_TAX_RE = re.compile(r"(налог|\bип\b|усн|ндфл)", re.I)
_FOOD_RE = re.compile(
    r"(суп|рецепт|петух|соли|картошк|лапш|бульон|варить|ингредиент)",
    re.I,
)


@dataclass(frozen=True)
class HistoryTurn:
    role: str
    content: str


@dataclass(frozen=True)
class FollowUpResolution:
    kind: str
    target: str
    previous_user: str = ""
    previous_assistant: str = ""
    inject_context: bool = False


def _clip(text: str, limit: int) -> str:
    raw = (text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit].rstrip() + "…"


def prior_turns(
    history: tuple[HistoryTurn, ...] | list[HistoryTurn],
    current_text: str,
) -> tuple[str, str]:
    """Return (previous_user, previous_assistant) excluding the current user turn."""
    turns = list(history or ())
    current = (current_text or "").strip()
    if (
        turns
        and str(turns[-1].role).lower() == "user"
        and str(turns[-1].content or "").strip() == current
    ):
        turns = turns[:-1]
    prev_assistant = ""
    prev_user = ""
    for turn in reversed(turns):
        role = str(turn.role or "").lower()
        content = str(turn.content or "").strip()
        if not content:
            continue
        if not prev_assistant and role == "assistant":
            if is_internal_assistant_text(content):
                continue
            prev_assistant = content
            continue
        if prev_assistant and not prev_user and role == "user":
            prev_user = content
            break
    return prev_user, prev_assistant


def _is_transform_only(text: str) -> bool:
    remainder = _TRANSFORM_RE.sub(" ", text or "")
    remainder = re.sub(r"[\s?!,.;:]+", " ", remainder).strip()
    remainder = re.sub(
        r"\b(пожалуйста|please|ответь|сделай|дай)\b",
        " ",
        remainder,
        flags=re.I,
    )
    remainder = re.sub(r"\s+", " ", remainder).strip()
    return len(remainder) <= 2 or len(remainder.split()) <= 2


def _domains(text: str) -> frozenset[str]:
    raw = text or ""
    found: set[str] = set()
    if _MARKETPLACE_RE.search(raw):
        found.add("marketplace")
    if _TAX_RE.search(raw):
        found.add("tax")
    if _FOOD_RE.search(raw):
        found.add("food")
    return frozenset(found)


def _domain_shift(current: str, previous_blob: str) -> bool:
    cur = _domains(current)
    prev = _domains(previous_blob)
    if not cur or not prev:
        return False
    return cur.isdisjoint(prev)


def resolve_follow_up(
    current_text: str,
    history: tuple[HistoryTurn, ...] | list[HistoryTurn] = (),
) -> FollowUpResolution:
    current = (current_text or "").strip()
    prev_user, prev_assistant = prior_turns(history, current)
    has_assistant = bool(prev_assistant)
    prior_blob = f"{prev_user}\n{prev_assistant}"
    tokens = len(current.split())

    if _NEW_TOPIC_RE.search(current) and _NEW_TASK_RE.search(current):
        return FollowUpResolution(KIND_NEW_TOPIC, TARGET_NONE, prev_user, prev_assistant, False)
    if has_assistant and _domain_shift(current, prior_blob) and tokens >= 4:
        return FollowUpResolution(KIND_NEW_TOPIC, TARGET_NONE, prev_user, prev_assistant, False)

    if _TRANSFORM_RE.search(current) and (
        _is_transform_only(current) or not _NEW_TASK_RE.search(current)
    ):
        if not has_assistant:
            return FollowUpResolution(KIND_MISSING_CONTEXT, TARGET_NONE, "", "", False)
        return FollowUpResolution(
            KIND_TRANSFORM, TARGET_PREVIOUS_ASSISTANT, prev_user, prev_assistant, True
        )
    if _REFERENT_RE.search(current):
        if not has_assistant:
            return FollowUpResolution(KIND_MISSING_CONTEXT, TARGET_NONE, "", "", False)
        return FollowUpResolution(
            KIND_REFERENT, TARGET_PREVIOUS_OPTIONS, prev_user, prev_assistant, True
        )
    if _QUESTION_FOLLOW_RE.search(current) or (
        tokens <= 8 and _DEICTIC_RE.search(current) and not _NEW_TASK_RE.search(current)
    ):
        if not has_assistant:
            return FollowUpResolution(KIND_MISSING_CONTEXT, TARGET_NONE, "", "", False)
        target = TARGET_PREVIOUS_FACT if re.search(r"почему|откуда", current, re.I) else TARGET_PREVIOUS_TOPIC
        return FollowUpResolution(KIND_QUESTION, target, prev_user, prev_assistant, True)

    if has_assistant and tokens <= 6 and not _NEW_TASK_RE.search(current):
        # Short underspecified follow-up ("пометь главное" covered above; leftovers like "риски?")
        if re.search(r"главн|короч|риски\??$|продолж", current, re.I):
            return FollowUpResolution(
                KIND_TRANSFORM, TARGET_PREVIOUS_ASSISTANT, prev_user, prev_assistant, True
            )

    return FollowUpResolution(KIND_STANDALONE, TARGET_NONE, prev_user, prev_assistant, False)


def build_follow_up_prompt(current_text: str, resolution: FollowUpResolution) -> str:
    current = (current_text or "").strip()
    if resolution.kind == KIND_MISSING_CONTEXT:
        return (
            "There is no previous assistant answer in this conversation.\n"
            "If the user asks to rewrite or shorten text, briefly ask them to provide the text.\n\n"
            f"CURRENT USER REQUEST:\n{current}"
        )
    if not resolution.inject_context:
        return current

    prev_user = _clip(resolution.previous_user, MAX_PRIOR_USER_CHARS)
    prev_asst = _clip(resolution.previous_assistant, MAX_PRIOR_ASSISTANT_CHARS)
    transform_note = ""
    if resolution.kind == KIND_TRANSFORM:
        transform_note = (
            "The user wants you to transform the previous assistant answer. "
            "Do not ask them to resend the text. Use PREVIOUS ASSISTANT ANSWER below.\n"
        )
    freshness_note = (
        "Numbers, tariffs, and dates in previous turns are conversational context only. "
        "Do not treat them as verified current data.\n"
    )
    parts = [
        "CONVERSATION CONTEXT (bounded; user-visible turns only).",
        transform_note.strip(),
        freshness_note.strip(),
        f"PREVIOUS USER:\n{prev_user}" if prev_user else "",
        f"PREVIOUS ASSISTANT ANSWER:\n{prev_asst}" if prev_asst else "",
        f"CURRENT USER REQUEST:\n{current}",
    ]
    return "\n\n".join(p for p in parts if p)
