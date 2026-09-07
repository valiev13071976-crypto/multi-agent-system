"""Resolves a stored UserPreferences record into ONE canonical
ResponseStyleProfile, built fresh on every turn (never cached across turns,
so a mid-conversation preference change applies prospectively starting with
the very next response — Block 4.28.6).
"""

from __future__ import annotations

from personalization.models import (
    LANGUAGE_AUTO,
    LANGUAGE_EN,
    LANGUAGE_RU,
    LENGTH_BALANCED,
    LENGTH_CONCISE,
    LENGTH_DETAILED,
    ResponseStyleProfile,
    STYLE_CONCISE,
    STYLE_DEFAULT,
    STYLE_DETAILED,
    STYLE_FRIENDLY,
    STYLE_PROFESSIONAL,
    TONE_AUTO,
    TONE_CONVERSATIONAL,
    TONE_FORMAL,
    TONE_FRIENDLY,
    TONE_NEUTRAL,
    UserPreferences,
    VALID_TONES,
)

_DEFAULT_TONE_BY_STYLE = {
    STYLE_DEFAULT: TONE_NEUTRAL,
    STYLE_PROFESSIONAL: TONE_FORMAL,
    STYLE_FRIENDLY: TONE_FRIENDLY,
    STYLE_CONCISE: TONE_NEUTRAL,
    STYLE_DETAILED: TONE_NEUTRAL,
}

_STYLE_LABEL_RU = {
    STYLE_DEFAULT: "стандартный, сбалансированный",
    STYLE_PROFESSIONAL: "деловой",
    STYLE_FRIENDLY: "дружелюбный",
    STYLE_CONCISE: "лаконичный",
    STYLE_DETAILED: "подробный",
}
_TONE_LABEL_RU = {
    TONE_NEUTRAL: "нейтральный",
    TONE_FORMAL: "формальный",
    TONE_CONVERSATIONAL: "разговорный",
    TONE_FRIENDLY: "дружелюбный",
}
_LENGTH_LABEL_RU = {
    LENGTH_CONCISE: "кратко и по существу",
    LENGTH_BALANCED: "сбалансированно по длине",
    LENGTH_DETAILED: "подробно, с деталями",
}

# Belt-and-suspenders (Block 4.28.7): even though style/tone/length/language
# never reach the tool-routing/HITL/canned-tool-result code paths (see
# WorkflowPandaConversationGateway.respond -- this directive is only ever
# appended to the free-text conversational `prompt`, never to a CALL_TOOL
# reply), the directive text ITSELF also explicitly tells the model it may
# not use presentation preferences to skip a required warning/check.
_SAFETY_CLAUSE_RU = (
    "Эти пользовательские настройки стиля общения касаются только тона и "
    "оформления ответа. Они не отменяют обязательные предупреждения, "
    "проверки безопасности, требования подтверждения действий или "
    "фактическую точность ответа."
)
_SAFETY_CLAUSE_EN = (
    "These user communication-style preferences affect tone and "
    "presentation only. They never override required warnings, safety "
    "checks, action-confirmation requirements, or factual accuracy."
)


def resolve_tone(style: str, tone: str) -> str:
    if tone in VALID_TONES and tone != TONE_AUTO:
        return tone
    return _DEFAULT_TONE_BY_STYLE.get(style, TONE_NEUTRAL)


def resolve_language(language: str, *, active_user_language_hint: str = LANGUAGE_RU) -> str:
    if language in (LANGUAGE_RU, LANGUAGE_EN):
        return language
    # LANGUAGE_AUTO (or any unexpected stored value, resolved deterministically
    # rather than guessed) -- follow the active user's language/context.
    hint = str(active_user_language_hint or "").strip().lower()
    return LANGUAGE_EN if hint.startswith("en") else LANGUAGE_RU


def build_style_profile(
    prefs: UserPreferences, *, active_user_language_hint: str = LANGUAGE_RU
) -> ResponseStyleProfile:
    tone = resolve_tone(prefs.style, prefs.tone)
    language = resolve_language(prefs.language, active_user_language_hint=active_user_language_hint)

    if language == LANGUAGE_EN:
        directive = (
            f"Style guidance: {prefs.style} style, {tone} tone, "
            f"{prefs.length} length. Respond in English. {_SAFETY_CLAUSE_EN}"
        )
    else:
        style_label = _STYLE_LABEL_RU.get(prefs.style, prefs.style)
        tone_label = _TONE_LABEL_RU.get(tone, tone)
        length_label = _LENGTH_LABEL_RU.get(prefs.length, prefs.length)
        directive = (
            f"Стиль общения: {style_label}. Тон: {tone_label}. "
            f"Детальность ответа: {length_label}. Отвечай на русском языке. "
            f"{_SAFETY_CLAUSE_RU}"
        )

    return ResponseStyleProfile(
        style=prefs.style,
        tone=tone,
        length=prefs.length,
        language=language,
        directive_text=directive,
    )
