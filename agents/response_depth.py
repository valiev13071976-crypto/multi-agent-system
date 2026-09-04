"""Message-level response depth — independent of task complexity and capabilities.

TaskRequirements.complexity remains computational/capability complexity.
This module only answers: how deep/structured should the user-facing reply be.
"""

from __future__ import annotations

import re

DEPTH_DIRECT = "direct"
DEPTH_NORMAL = "normal"
DEPTH_ANALYTICAL = "analytical"
DEPTH_DEEP = "deep"

RESPONSE_DEPTH_LEVELS = (
    DEPTH_DIRECT,
    DEPTH_NORMAL,
    DEPTH_ANALYTICAL,
    DEPTH_DEEP,
)

# Presentation depth does not change Pipeline execution in this block.
ORCHESTRATION_FULL_PIPELINE = "full_pipeline"

_DEPTH_RANK = {
    DEPTH_DIRECT: 0,
    DEPTH_NORMAL: 1,
    DEPTH_ANALYTICAL: 2,
    DEPTH_DEEP: 3,
}
_RANK_TO_DEPTH = {rank: name for name, rank in _DEPTH_RANK.items()}

STRATEGIST_FRAMEWORK_MARKERS = (
    "Ключевой вывод",
    "Возможные варианты действий",
    "Рекомендуемая стратегия",
    "Необходимые ресурсы и расчёты",
    "Основные риски",
)

_BREVITY_RE = re.compile(
    r"(ответь\s+коротко|коротко|кратко|одним\s+предложением|"
    r"in\s+one\s+sentence|briefly|tl;?\s*dr)",
    re.I,
)
_DEPTH_UP_RE = re.compile(
    r"(подробно|разберись\s+глубоко|подробн\w*\s+анализ|детальн|"
    r"глубокий\s+анализ|in[- ]depth|comprehensively|разбери\s+подробно)",
    re.I,
)

_SMALL_TALK_RES = (
    re.compile(
        r"\b(привет|здравствуй(?:те)?|добро(?:е|го|ый)\s+"
        r"(?:утро|день|вечер)|hello|hi|hey|хай)\b",
        re.I,
    ),
    re.compile(r"\b(спасибо|благодар(?:ю|им)|thanks|thank\s+you|пожалуйста)\b", re.I),
    re.compile(r"\b(пока|до\s+свидания|goodbye|bye|увидимся)\b", re.I),
    re.compile(
        r"(как\s+(?:дела|ты|поживаешь)|how\s+are\s+you|что\s+нового)",
        re.I,
    ),
    re.compile(r"^\s*(ок|ok|okay|понял(?:а)?|ясно|хорошо)\s*[.!]?\s*$", re.I),
)

_ADVISORY_RE = re.compile(
    r"(помоги\s+выбрать|выбрать\s+между|плюсы\s+и\s+минусы|"
    r"сравни|vs\.?|trade-?off|что\s+лучше|или\s+\w+\s+для)",
    re.I,
)

_ANALYZE_RE = re.compile(r"(проанализ|разработ|предложи\s+стратег|экономик)", re.I)
_FACTOR_RES = (
    re.compile(r"комисс", re.I),
    re.compile(r"логист", re.I),
    re.compile(r"реклам", re.I),
    re.compile(r"\bндс\b", re.I),
    re.compile(r"налог", re.I),
    re.compile(r"запуск", re.I),
)


def normalize_response_depth(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in RESPONSE_DEPTH_LEVELS:
        return raw
    return DEPTH_NORMAL


def contains_strategist_framework(text: str) -> bool:
    body = text or ""
    return all(marker in body for marker in STRATEGIST_FRAMEWORK_MARKERS)


def has_brevity_instruction(text: str) -> bool:
    return bool(_BREVITY_RE.search(text or ""))


def has_depth_instruction(text: str) -> bool:
    return bool(_DEPTH_UP_RE.search(text or ""))


def orchestration_policy_for(_depth: str) -> str:
    """Orchestration is recorded separately from presentation depth.

    This block keeps the existing Pipeline (experts → peer/fact → Judge).
    Reducing stages by depth is a documented P2 follow-up.
    """
    return ORCHESTRATION_FULL_PIPELINE


def _shift_depth(depth: str, delta: int) -> str:
    rank = _DEPTH_RANK[normalize_response_depth(depth)]
    return _RANK_TO_DEPTH[max(0, min(3, rank + delta))]


def _matched_small_talk(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SMALL_TALK_RES)


def is_small_talk_only(text: str) -> bool:
    """True when the message is a conversational act, not a task clause."""
    raw = (text or "").strip()
    if not raw or not _matched_small_talk(raw):
        return False
    remainder = raw
    for pattern in _SMALL_TALK_RES:
        remainder = pattern.sub(" ", remainder)
    remainder = re.sub(r"[\s?!,.;:…\-—]+", " ", remainder).strip()
    remainder = re.sub(r"\b(?:а|ну|эм|hmm|um)\b", " ", remainder, flags=re.I)
    remainder = re.sub(r"\s+", " ", remainder).strip()
    return len(remainder) <= 2


def _is_advisory(text: str) -> bool:
    return bool(_ADVISORY_RE.search(text or ""))


def _is_multi_factor_analysis(text: str) -> bool:
    raw = text or ""
    if not _ANALYZE_RE.search(raw):
        return False
    hits = sum(1 for pattern in _FACTOR_RES if pattern.search(raw))
    return hits >= 3


def classify_response_depth(
    user_request: str,
    *,
    category: str | None = None,
    role_id: str | None = None,
) -> str:
    """Deterministic message-level depth. Uncertain → NORMAL, never DIRECT/DEEP.

    ``category`` must be the message TaskClassifier category, not a role-mapped
    routing category. ``role_id`` is unused for depth (role ≠ presentation).
    """
    del role_id  # role is expertise, not depth
    text = user_request if isinstance(user_request, str) else str(user_request)
    category_key = str(category or "").strip() or None

    brevity = has_brevity_instruction(text)
    deeper = has_depth_instruction(text)

    if is_small_talk_only(text) and category_key not in {
        "strategy",
        "critique",
        "research",
        "trend_analysis",
        "technical",
    }:
        return DEPTH_DIRECT

    if category_key == "strategy":
        base = DEPTH_DEEP
    elif _is_multi_factor_analysis(text):
        base = DEPTH_DEEP
    elif _is_advisory(text):
        base = DEPTH_ANALYTICAL
    elif category_key in {
        "critique",
        "research",
        "trend_analysis",
        "technical",
    }:
        base = DEPTH_ANALYTICAL
    else:
        # general / unknown / simple questions
        base = DEPTH_NORMAL

    if brevity and not deeper:
        return _shift_depth(base, -1)
    if deeper and not brevity:
        return _shift_depth(base, 1)
    return base
