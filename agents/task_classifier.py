from dataclasses import dataclass
import re
from typing import Mapping

from agents.routing_requirements import (
    TaskRequirements,
    derive_task_requirements,
)


CATEGORY_STRATEGY = "strategy"
CATEGORY_CRITIQUE = "critique"
CATEGORY_RESEARCH = "research"
CATEGORY_TREND_ANALYSIS = "trend_analysis"
CATEGORY_TECHNICAL = "technical"
CATEGORY_GENERAL = "general"

ROLE_STRATEGIST = "strategist"
ROLE_CRITIC = "critic"
ROLE_RESEARCHER = "researcher"
ROLE_TREND_AGENT = "trend_agent"
ROLE_TECHNICAL = "technical"

REASON_TECHNICAL_ARTIFACT = "technical_artifact"
REASON_CRITIQUE_INTENT = "critique_intent"
REASON_TREND_INTENT = "trend_intent"
REASON_RESEARCH_INTENT = "research_intent"
REASON_STRATEGY_INTENT = "strategy_intent"
REASON_GENERAL_FALLBACK = "general_fallback"

CATEGORY_TO_ROLE = {
    CATEGORY_STRATEGY: ROLE_STRATEGIST,
    CATEGORY_CRITIQUE: ROLE_CRITIC,
    CATEGORY_RESEARCH: ROLE_RESEARCHER,
    CATEGORY_TREND_ANALYSIS: ROLE_TREND_AGENT,
    CATEGORY_TECHNICAL: ROLE_TECHNICAL,
    CATEGORY_GENERAL: ROLE_STRATEGIST,
}

CONFIDENCE_STRONG = 0.90
CONFIDENCE_SEMANTIC = 0.70
CONFIDENCE_FALLBACK = 0.50

FENCED_CODE_RE = re.compile(r"```")
TRACEBACK_RE = re.compile(r"traceback \(most recent call last\)", re.I)
STACK_TRACE_RE = re.compile(r"\bstack trace\b", re.I)
SOURCE_FILE_RE = re.compile(
    r"""(?:^|[\s"'`])"""
    r"(?:dockerfile|[\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java))\b",
    re.I,
)
PYTEST_RE = re.compile(r"\bpytest\b", re.I)
EXCEPTION_RE = re.compile(
    r"\b(?:TypeError|ValueError|RuntimeError|KeyError|"
    r"AttributeError|ImportError|SyntaxError|NameError)\b"
)
CODE_INTENT_RE = re.compile(
    r"\b(?:debug|refactor|traceback|stacktrace|юнит-тест|unit tests?)\b",
    re.I,
)

CRITIQUE_PHRASES = (
    "найди ошибки",
    "слабые места",
    "критически оцени",
    "критически оценить",
    "проверь решение на риски",
    "проверить решение на риски",
    "code review",
    "код-ревью",
    "код ревью",
)

TREND_PHRASES = (
    "динамика рынка",
    "прогноз спроса",
    "рыночный тренд",
    "рыночные тренды",
    "изменение рынка",
    "изменения рынка",
)

TREND_WORDS = (
    "тренды",
    "тренд",
)

RESEARCH_PHRASES = (
    "проверь факты",
    "проверить факты",
    "найди данные",
    "фактчек",
    "факт-чек",
    "fact check",
    "factcheck",
)

RESEARCH_WORDS = (
    "источники",
    "исследования",
    "evidence",
    "подтверждения",
)

STRATEGY_PHRASES = (
    "бизнес-модель",
    "бизнес модель",
    "план развития",
    "стратегия продаж",
    "стратегию продаж",
    "roadmap",
)

STRATEGY_WORDS = (
    "стратегия",
    "стратегию",
    "стратегии",
)


@dataclass(frozen=True)
class TaskClassification:
    category: str
    role_id: str
    confidence: float
    reason: str
    requirements: TaskRequirements | None = None

    def __post_init__(self):
        if self.requirements is None:
            object.__setattr__(
                self,
                "requirements",
                derive_task_requirements(category=self.category),
            )


def _normalize(user_request: str) -> str:
    return user_request if isinstance(user_request, str) else str(user_request)


def _count_hits(text: str, phrases: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(1 for phrase in phrases if phrase in lowered)


def _technical_hit(text: str) -> bool:
    return bool(
        FENCED_CODE_RE.search(text)
        or TRACEBACK_RE.search(text)
        or STACK_TRACE_RE.search(text)
        or SOURCE_FILE_RE.search(text)
        or PYTEST_RE.search(text)
        or EXCEPTION_RE.search(text)
        or CODE_INTENT_RE.search(text)
    )


def _result(
    category: str,
    reason: str,
    confidence: float,
    *,
    text: str = "",
    metadata: Mapping | None = None,
) -> TaskClassification:
    return TaskClassification(
        category=category,
        role_id=CATEGORY_TO_ROLE[category],
        confidence=confidence,
        reason=reason,
        requirements=derive_task_requirements(
            category=category,
            text=text,
            metadata=metadata,
        ),
    )


class TaskClassifier:
    """
    Deterministic offline task classifier.
    Produces category/role plus associated TaskRequirements.
    Wired into RouterV2 when role == "auto".
    """

    def classify(
        self,
        user_request: str,
        metadata: Mapping | None = None,
    ) -> TaskClassification:
        text = _normalize(user_request)

        if _technical_hit(text):
            return _result(
                CATEGORY_TECHNICAL,
                REASON_TECHNICAL_ARTIFACT,
                CONFIDENCE_STRONG,
                text=text,
                metadata=metadata,
            )

        critique_hits = _count_hits(text, CRITIQUE_PHRASES)
        if critique_hits:
            confidence = (
                CONFIDENCE_SEMANTIC if critique_hits >= 2 else CONFIDENCE_STRONG
            )
            return _result(
                CATEGORY_CRITIQUE,
                REASON_CRITIQUE_INTENT,
                confidence,
                text=text,
                metadata=metadata,
            )

        trend_phrase_hits = _count_hits(text, TREND_PHRASES)
        trend_word_hits = _count_hits(text, TREND_WORDS)
        if trend_phrase_hits or (
            trend_word_hits and ("рынок" in text.lower() or "спрос" in text.lower())
        ):
            confidence = (
                CONFIDENCE_SEMANTIC
                if trend_phrase_hits + trend_word_hits >= 2
                else CONFIDENCE_STRONG
            )
            return _result(
                CATEGORY_TREND_ANALYSIS,
                REASON_TREND_INTENT,
                confidence,
                text=text,
                metadata=metadata,
            )

        research_hits = _count_hits(text, RESEARCH_PHRASES) + _count_hits(
            text, RESEARCH_WORDS
        )
        if research_hits:
            confidence = (
                CONFIDENCE_SEMANTIC if research_hits >= 2 else CONFIDENCE_STRONG
            )
            return _result(
                CATEGORY_RESEARCH,
                REASON_RESEARCH_INTENT,
                confidence,
                text=text,
                metadata=metadata,
            )

        strategy_hits = _count_hits(text, STRATEGY_PHRASES) + _count_hits(
            text, STRATEGY_WORDS
        )
        if strategy_hits:
            confidence = (
                CONFIDENCE_SEMANTIC if strategy_hits >= 2 else CONFIDENCE_STRONG
            )
            return _result(
                CATEGORY_STRATEGY,
                REASON_STRATEGY_INTENT,
                confidence,
                text=text,
                metadata=metadata,
            )

        return _result(
            CATEGORY_GENERAL,
            REASON_GENERAL_FALLBACK,
            CONFIDENCE_FALLBACK,
            text=text,
            metadata=metadata,
        )


def classify_task(
    user_request: str,
    metadata: Mapping | None = None,
) -> TaskClassification:
    return TaskClassifier().classify(user_request, metadata=metadata)
