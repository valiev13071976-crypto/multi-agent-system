"""Personalization contracts — response style/tone/length/language + voice.

A style/tone/length/language/voice preference is a PRESENTATION setting
only. It is resolved into a short natural-language directive appended to
the prompt for the ordinary free-text conversational branch (see
business_assistant.conversation_gateway.WorkflowPandaConversationGateway.
respond) and never reaches tool-call routing, HITL, authorization, or
canned tool-result text (business_assistant.action_continuation.
format_tool_user_text) -- see personalization/resolver.py and Block 4.28.7.
"""

from __future__ import annotations

from dataclasses import dataclass

STYLE_DEFAULT = "default"
STYLE_PROFESSIONAL = "professional"
STYLE_FRIENDLY = "friendly"
STYLE_CONCISE = "concise"
STYLE_DETAILED = "detailed"
VALID_STYLES = frozenset(
    {STYLE_DEFAULT, STYLE_PROFESSIONAL, STYLE_FRIENDLY, STYLE_CONCISE, STYLE_DETAILED}
)

TONE_NEUTRAL = "neutral"
TONE_FORMAL = "formal"
TONE_CONVERSATIONAL = "conversational"
TONE_FRIENDLY = "friendly"
VALID_TONES = frozenset({TONE_NEUTRAL, TONE_FORMAL, TONE_CONVERSATIONAL, TONE_FRIENDLY})
# Empty string means "derive tone from style" (see resolver.resolve_tone) --
# never a contradictory user-picked combination silently invented for them.
TONE_AUTO = ""

LENGTH_CONCISE = "concise"
LENGTH_BALANCED = "balanced"
LENGTH_DETAILED = "detailed"
VALID_LENGTHS = frozenset({LENGTH_CONCISE, LENGTH_BALANCED, LENGTH_DETAILED})

LANGUAGE_RU = "ru"
LANGUAGE_EN = "en"
LANGUAGE_AUTO = "auto"
VALID_LANGUAGES = frozenset({LANGUAGE_RU, LANGUAGE_EN, LANGUAGE_AUTO})

# Real provider voice IDs (integrations.production.adapters.speech.
# OpenAITextToSpeechProvider / ui_chat.voice.tts.FakeTextToSpeechProvider both
# accept an opaque `voice: str` -- these are the literal IDs passed through
# unchanged to `TextToSpeechProvider.synthesize(voice=...)`). "category" is a
# UX-only presentation label for the settings screen; it is not a claim about
# the provider's own documentation of voice gender, per Block 4.29 ("Do not
# claim biological gender for synthetic voices").
CATEGORY_FEMININE = "feminine"
CATEGORY_MASCULINE = "masculine"
CATEGORY_NEUTRAL = "neutral"


@dataclass(frozen=True)
class VoiceCatalogEntry:
    voice_id: str
    label: str
    category: str  # one of CATEGORY_*


VOICE_CATALOG: tuple[VoiceCatalogEntry, ...] = (
    VoiceCatalogEntry("alloy", "Alloy (нейтральный)", CATEGORY_NEUTRAL),
    VoiceCatalogEntry("nova", "Nova (женский)", CATEGORY_FEMININE),
    VoiceCatalogEntry("shimmer", "Shimmer (женский)", CATEGORY_FEMININE),
    VoiceCatalogEntry("echo", "Echo (мужской)", CATEGORY_MASCULINE),
    VoiceCatalogEntry("onyx", "Onyx (мужской)", CATEGORY_MASCULINE),
    VoiceCatalogEntry("fable", "Fable (нейтральный)", CATEGORY_NEUTRAL),
)
VALID_VOICE_IDS = frozenset(entry.voice_id for entry in VOICE_CATALOG)
DEFAULT_VOICE_ID = "alloy"

# One short, fixed preview phrase -- Block 4.29.1 forbids an automatic/paid
# preview loop; each preview is exactly one explicit user-triggered
# synthesize() call for this fixed text.
VOICE_PREVIEW_TEXT = "Здравствуйте! Это пример голоса Панды."


@dataclass(frozen=True)
class UserPreferences:
    tenant_id: str
    owner_id: str
    style: str = STYLE_DEFAULT
    tone: str = TONE_AUTO
    length: str = LENGTH_BALANCED
    language: str = LANGUAGE_AUTO
    voice_id: str = DEFAULT_VOICE_ID
    updated_at: str = ""


@dataclass(frozen=True)
class ResponseStyleProfile:
    """Canonical resolved profile — built ONCE per turn, before the
    free-text conversational model invocation (Block 4.28.2)."""

    style: str
    tone: str
    length: str
    language: str
    directive_text: str
