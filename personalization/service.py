"""Personalization service — canonical facade for preference read/write,
style-profile resolution, voice catalog listing, and voice preview.

Reuses the existing STT/TTS provider wiring
(integrations.production.adapters.speech.build_speech_providers) for voice
preview -- no parallel TTS implementation.
"""

from __future__ import annotations

from security.tenant import require_tenant_id
from ui_chat.voice.tts import TextToSpeechProvider

from personalization.errors import (
    PZ_INVALID_LANGUAGE,
    PZ_INVALID_LENGTH,
    PZ_INVALID_STYLE,
    PZ_INVALID_TONE,
    PZ_INVALID_VOICE,
    PZ_PREVIEW_FAILED,
    PersonalizationError,
)
from personalization.models import (
    DEFAULT_VOICE_ID,
    LANGUAGE_AUTO,
    LENGTH_BALANCED,
    ResponseStyleProfile,
    STYLE_DEFAULT,
    TONE_AUTO,
    UserPreferences,
    VALID_LANGUAGES,
    VALID_LENGTHS,
    VALID_STYLES,
    VALID_TONES,
    VALID_VOICE_IDS,
    VOICE_CATALOG,
    VOICE_PREVIEW_TEXT,
    VoiceCatalogEntry,
)
from personalization.resolver import build_style_profile
from personalization.store import SqlitePersonalizationStore


class PersonalizationService:
    def __init__(self, *, store: SqlitePersonalizationStore, tts: TextToSpeechProvider):
        self.store = store
        self.tts = tts

    def close(self) -> None:
        self.store.close()

    def get_preferences(self, *, tenant_id: str, owner_id: str) -> UserPreferences:
        tenant = require_tenant_id(tenant_id)
        owner = str(owner_id or "").strip()
        existing = self.store.get(tenant_id=tenant, owner_id=owner)
        if existing is not None:
            return existing
        # Block 4.31: no saved preference resolves deterministically to the
        # safe default profile — equivalent to current (pre-Block-4) Panda
        # behavior (no style directive, balanced length, auto language).
        return UserPreferences(tenant_id=tenant, owner_id=owner)

    def set_preferences(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        style: str | None = None,
        tone: str | None = None,
        length: str | None = None,
        language: str | None = None,
        voice_id: str | None = None,
    ) -> UserPreferences:
        tenant = require_tenant_id(tenant_id)
        owner = str(owner_id or "").strip()
        current = self.get_preferences(tenant_id=tenant, owner_id=owner)

        next_style = current.style if style is None else str(style).strip().lower()
        if next_style not in VALID_STYLES:
            raise PersonalizationError(PZ_INVALID_STYLE, f"invalid_style:{next_style}")

        next_tone = current.tone if tone is None else str(tone).strip().lower()
        if next_tone != TONE_AUTO and next_tone not in VALID_TONES:
            raise PersonalizationError(PZ_INVALID_TONE, f"invalid_tone:{next_tone}")

        next_length = current.length if length is None else str(length).strip().lower()
        if next_length not in VALID_LENGTHS:
            raise PersonalizationError(PZ_INVALID_LENGTH, f"invalid_length:{next_length}")

        next_language = current.language if language is None else str(language).strip().lower()
        if next_language not in VALID_LANGUAGES:
            raise PersonalizationError(PZ_INVALID_LANGUAGE, f"invalid_language:{next_language}")

        next_voice = current.voice_id if voice_id is None else str(voice_id).strip().lower()
        if next_voice not in VALID_VOICE_IDS:
            raise PersonalizationError(PZ_INVALID_VOICE, f"invalid_voice:{next_voice}")

        updated = UserPreferences(
            tenant_id=tenant,
            owner_id=owner,
            style=next_style,
            tone=next_tone,
            length=next_length,
            language=next_language,
            voice_id=next_voice,
        )
        # Block 4.28.6 / 4.29.2: this is a pure preference write. It never
        # touches conversation storage -- no new conversation, no message
        # mutation, no re-run of prior turns.
        return self.store.upsert(updated)

    def resolve_style_profile(
        self, *, tenant_id: str, owner_id: str, active_user_language_hint: str = "ru"
    ) -> ResponseStyleProfile:
        prefs = self.get_preferences(tenant_id=tenant_id, owner_id=owner_id)
        return build_style_profile(prefs, active_user_language_hint=active_user_language_hint)

    def list_voices(self) -> tuple[VoiceCatalogEntry, ...]:
        return VOICE_CATALOG

    def preview_voice(self, *, voice_id: str) -> tuple[bytes, str]:
        vid = str(voice_id or "").strip().lower()
        if vid not in VALID_VOICE_IDS:
            raise PersonalizationError(PZ_INVALID_VOICE, f"invalid_voice:{vid}", http_status=422)
        try:
            # Exactly one explicit, user-triggered synthesize() call for a
            # short fixed phrase -- Block 4.29.1 forbids an automatic/paid
            # preview loop.
            blob = self.tts.synthesize(text=VOICE_PREVIEW_TEXT, voice=vid, mime_type="audio/wav")
        except Exception as exc:
            raise PersonalizationError(PZ_PREVIEW_FAILED, str(exc), http_status=422) from exc
        return blob, "audio/wav"
