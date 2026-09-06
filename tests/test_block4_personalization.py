"""Block 4.28/4.29 — personalization (response style/tone/length/language)
and voice-selection contract tests. Offline/fake-provider only.

Covers the PERSONALIZATION TEST CONTRACT (master spec section 54, A-I).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from personalization.errors import PersonalizationError
from personalization.models import (
    DEFAULT_VOICE_ID,
    LANGUAGE_EN,
    LANGUAGE_RU,
    LENGTH_CONCISE,
    LENGTH_DETAILED,
    STYLE_DEFAULT,
    STYLE_FRIENDLY,
    STYLE_PROFESSIONAL,
    VOICE_CATALOG,
)
from personalization.resolver import build_style_profile, resolve_language, resolve_tone
from personalization.service import PersonalizationService
from personalization.store import SqlitePersonalizationStore
from ui_chat.voice.tts import FakeTextToSpeechProvider


class PersonalizationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = SqlitePersonalizationStore(os.path.join(self.tmp, "pz.sqlite"))
        self.tts = FakeTextToSpeechProvider()
        self.svc = PersonalizationService(store=self.store, tts=self.tts)

    def tearDown(self):
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- 4.31 defaults / backward compatibility -----------------------------

    def test_no_saved_preference_resolves_to_safe_defaults(self):
        prefs = self.svc.get_preferences(tenant_id="t1", owner_id="u1")
        self.assertEqual(prefs.style, STYLE_DEFAULT)
        self.assertEqual(prefs.voice_id, DEFAULT_VOICE_ID)
        profile = self.svc.resolve_style_profile(tenant_id="t1", owner_id="u1")
        self.assertEqual(profile.style, STYLE_DEFAULT)

    # --- Contract A/B/C: style/tone/length change resolved profile ---------

    def test_a_professional_style_resolves_into_profile(self):
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", style=STYLE_PROFESSIONAL)
        profile = self.svc.resolve_style_profile(tenant_id="t1", owner_id="u1")
        self.assertEqual(profile.style, STYLE_PROFESSIONAL)
        self.assertEqual(profile.tone, "formal")
        self.assertIn("деловой", profile.directive_text)

    def test_b_friendly_style_changes_presentation_profile_not_authorization(self):
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", style=STYLE_FRIENDLY)
        profile = self.svc.resolve_style_profile(tenant_id="t1", owner_id="u1")
        self.assertEqual(profile.style, STYLE_FRIENDLY)
        self.assertNotEqual(profile.tone, "formal")
        # Presentation-only: the profile object carries no authorization/tool
        # fields at all (there is nothing here to bypass permissions with).
        self.assertFalse(hasattr(profile, "permissions"))
        self.assertFalse(hasattr(profile, "tool_id"))

    def test_c_length_preference_changes_resolved_profile(self):
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", length=LENGTH_CONCISE)
        p1 = self.svc.resolve_style_profile(tenant_id="t1", owner_id="u1")
        self.assertEqual(p1.length, LENGTH_CONCISE)
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", length=LENGTH_DETAILED)
        p2 = self.svc.resolve_style_profile(tenant_id="t1", owner_id="u1")
        self.assertEqual(p2.length, LENGTH_DETAILED)
        self.assertNotEqual(p1.directive_text, p2.directive_text)

    # --- Contract D: language preference ------------------------------------

    def test_d_language_ru_en_auto(self):
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", language=LANGUAGE_RU)
        self.assertEqual(
            self.svc.resolve_style_profile(tenant_id="t1", owner_id="u1").language, LANGUAGE_RU
        )
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", language=LANGUAGE_EN)
        profile_en = self.svc.resolve_style_profile(tenant_id="t1", owner_id="u1")
        self.assertEqual(profile_en.language, LANGUAGE_EN)
        self.assertIn("Respond in English", profile_en.directive_text)
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", language="auto")
        auto_ru = self.svc.resolve_style_profile(
            tenant_id="t1", owner_id="u1", active_user_language_hint="ru"
        )
        auto_en = self.svc.resolve_style_profile(
            tenant_id="t1", owner_id="u1", active_user_language_hint="en"
        )
        self.assertEqual(auto_ru.language, LANGUAGE_RU)
        self.assertEqual(auto_en.language, LANGUAGE_EN)

    def test_resolve_language_helper_direct(self):
        self.assertEqual(resolve_language(LANGUAGE_RU), LANGUAGE_RU)
        self.assertEqual(resolve_language("auto", active_user_language_hint="en-US"), LANGUAGE_EN)
        self.assertEqual(resolve_language("auto", active_user_language_hint="ru-RU"), LANGUAGE_RU)

    def test_resolve_tone_derives_from_style_when_unset(self):
        self.assertEqual(resolve_tone(STYLE_PROFESSIONAL, ""), "formal")
        self.assertEqual(resolve_tone(STYLE_DEFAULT, "conversational"), "conversational")

    # --- Contract E: reload / persistence -----------------------------------

    def test_e_reload_restores_persisted_preference(self):
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", style=STYLE_PROFESSIONAL, voice_id="onyx")
        # Simulate reload: brand-new service instance over the SAME db file.
        store2 = SqlitePersonalizationStore(os.path.join(self.tmp, "pz.sqlite"))
        svc2 = PersonalizationService(store=store2, tts=self.tts)
        try:
            prefs = svc2.get_preferences(tenant_id="t1", owner_id="u1")
            self.assertEqual(prefs.style, STYLE_PROFESSIONAL)
            self.assertEqual(prefs.voice_id, "onyx")
        finally:
            svc2.close()

    # --- Contract F: mid-conversation change, one canonical source ---------

    def test_f_preference_change_does_not_touch_conversation_storage(self):
        # set_preferences never receives/returns anything conversation-shaped;
        # it is a pure (tenant, owner) preference write (Block 4.28.6).
        before = self.svc.set_preferences(tenant_id="t1", owner_id="u1", style=STYLE_FRIENDLY)
        after = self.svc.set_preferences(tenant_id="t1", owner_id="u1", style=STYLE_PROFESSIONAL)
        self.assertEqual(before.tenant_id, after.tenant_id)
        self.assertEqual(before.owner_id, after.owner_id)
        self.assertNotEqual(before.style, after.style)
        # One row per (tenant, owner) -- not one row per turn/message.
        row = self.store.get(tenant_id="t1", owner_id="u1")
        self.assertEqual(row.style, STYLE_PROFESSIONAL)

    # --- Contract G/H: voice selection --------------------------------------

    def test_g_voice_selection_persists_and_is_returned(self):
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", voice_id="nova")
        prefs = self.svc.get_preferences(tenant_id="t1", owner_id="u1")
        self.assertEqual(prefs.voice_id, "nova")

    def test_h_voice_change_does_not_alter_other_fields(self):
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", style=STYLE_PROFESSIONAL, language=LANGUAGE_EN)
        self.svc.set_preferences(tenant_id="t1", owner_id="u1", voice_id="echo")
        prefs = self.svc.get_preferences(tenant_id="t1", owner_id="u1")
        self.assertEqual(prefs.style, STYLE_PROFESSIONAL)
        self.assertEqual(prefs.language, LANGUAGE_EN)
        self.assertEqual(prefs.voice_id, "echo")

    def test_voice_catalog_never_claims_biological_gender(self):
        for entry in VOICE_CATALOG:
            self.assertNotIn("биологич", entry.label.lower())

    def test_invalid_voice_rejected(self):
        with self.assertRaises(PersonalizationError):
            self.svc.set_preferences(tenant_id="t1", owner_id="u1", voice_id="not-a-real-voice")

    def test_invalid_style_tone_length_language_rejected(self):
        with self.assertRaises(PersonalizationError):
            self.svc.set_preferences(tenant_id="t1", owner_id="u1", style="sarcastic")
        with self.assertRaises(PersonalizationError):
            self.svc.set_preferences(tenant_id="t1", owner_id="u1", tone="shouting")
        with self.assertRaises(PersonalizationError):
            self.svc.set_preferences(tenant_id="t1", owner_id="u1", length="huge")
        with self.assertRaises(PersonalizationError):
            self.svc.set_preferences(tenant_id="t1", owner_id="u1", language="klingon")

    def test_tenant_isolation_preferences_never_cross_tenant(self):
        self.svc.set_preferences(tenant_id="tenant-a", owner_id="u1", style=STYLE_PROFESSIONAL)
        other = self.svc.get_preferences(tenant_id="tenant-b", owner_id="u1")
        self.assertEqual(other.style, STYLE_DEFAULT)

    # --- 4.29.1 voice preview: one explicit call, no loop -------------------

    def test_voice_preview_calls_synthesize_exactly_once(self):
        calls = []
        real_synth = self.tts.synthesize

        def counting_synth(*, text, voice="default", mime_type="audio/wav"):
            calls.append((text, voice))
            return real_synth(text=text, voice=voice, mime_type=mime_type)

        self.tts.synthesize = counting_synth
        blob, mime = self.svc.preview_voice(voice_id="nova")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "nova")
        self.assertTrue(blob)
        self.assertEqual(mime, "audio/wav")

    def test_voice_preview_rejects_unknown_voice(self):
        with self.assertRaises(PersonalizationError):
            self.svc.preview_voice(voice_id="does-not-exist")

    # --- 4.28.7 safety non-override, embedded in directive text ------------

    def test_style_directive_always_carries_safety_clause(self):
        for style in (STYLE_DEFAULT, STYLE_PROFESSIONAL, STYLE_FRIENDLY):
            self.svc.set_preferences(tenant_id="t1", owner_id="u1", style=style)
            profile = self.svc.resolve_style_profile(tenant_id="t1", owner_id="u1")
            self.assertIn("не отменяют", profile.directive_text)


class BuildStyleProfileUnitTests(unittest.TestCase):
    def test_directive_text_is_deterministic_for_same_input(self):
        from personalization.models import UserPreferences

        prefs = UserPreferences(tenant_id="t", owner_id="u", style=STYLE_PROFESSIONAL, language=LANGUAGE_RU)
        p1 = build_style_profile(prefs)
        p2 = build_style_profile(prefs)
        self.assertEqual(p1.directive_text, p2.directive_text)


if __name__ == "__main__":
    unittest.main()
