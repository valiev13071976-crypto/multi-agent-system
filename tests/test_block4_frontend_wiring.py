"""Block 4 — static frontend wiring smoke tests (structural, no browser).

Cheap sufficient verification for the realtime voice + personalization
settings UI: confirms the DOM contract app.js/realtime.js/personalization.js
depend on actually exists in index.html, the new scripts are loaded in the
right order, and the client-side files are syntactically self-consistent.
Full visual/interactive verification is out of scope here (Block 4.30/57:
no ChatGPT reference screenshot was supplied for this screen, so no 1:1
visual claim is made -- see the delivery report).
"""

from __future__ import annotations

import unittest
from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


class RealtimeVoiceUiWiringTests(unittest.TestCase):
    def setUp(self):
        self.html = _read("static/panda/index.html")
        self.app_js = _read("static/panda/js/app.js")
        self.realtime_js = _read("static/panda/js/realtime.js")

    def test_mic_button_and_live_caption_present(self):
        self.assertIn('id="mic-btn"', self.html)
        self.assertIn('id="voice-live-caption"', self.html)
        self.assertIn('id="realtime-audio-player"', self.html)

    def test_scripts_load_realtime_before_app(self):
        rt_idx = self.html.index('/static/panda/js/realtime.js')
        app_idx = self.html.index('/static/panda/js/app.js')
        self.assertLess(rt_idx, app_idx)

    def test_app_js_wires_mic_click_and_realtime_controller(self):
        self.assertIn('els.micBtn.onclick', self.app_js)
        self.assertIn('ensureRealtimeController', self.app_js)
        self.assertIn('RealtimeVoiceController', self.realtime_js)
        self.assertIn('onAssistantTextDelta', self.app_js)
        self.assertIn('onAssistantAudio', self.app_js)
        self.assertIn('onInterruption', self.app_js)
        self.assertIn('onUserTurnCommitted', self.app_js)

    def test_realtime_js_uses_canonical_ws_endpoint_and_dual_auth_query_param(self):
        self.assertIn('/api/v1/realtime/ws', self.realtime_js)
        self.assertIn('panda_api_key', self.realtime_js)

    def test_mic_hidden_when_unsupported(self):
        self.assertIn('PandaRealtime.isSupported()', self.app_js)

    def test_voice_session_closed_on_logout(self):
        self.assertIn('state.realtime.controller.close()', self.app_js)

    def test_voice_mode_bar_with_distinct_exit_and_mute_controls_present(self):
        # ChatGPT-voice-mode-parity defect closure: voice mode is a distinct
        # conversation surface (orb + mute + exit), not a plain
        # record/upload composer control, and mute/exit are two DIFFERENT
        # controls with different semantics (Block 4 section 6).
        self.assertIn('id="voice-mode-bar"', self.html)
        self.assertIn('id="voice-orb"', self.html)
        self.assertIn('id="voice-exit-btn"', self.html)
        self.assertIn('id="voice-mute-btn"', self.html)

    def test_app_js_wires_orb_mute_and_exit_as_distinct_controls(self):
        self.assertIn('els.voiceOrb.onclick = onVoiceOrbClick', self.app_js)
        self.assertIn('els.voiceMuteBtn.onclick = onVoiceMuteClick', self.app_js)
        self.assertIn('els.voiceExitBtn.onclick = onVoiceExitClick', self.app_js)
        self.assertIn('controller.isMuted', self.app_js)
        self.assertIn('controller.mute()', self.app_js)
        self.assertIn('controller.unmute()', self.app_js)
        self.assertIn('controller.close()', self.app_js)

    def test_continuous_voice_loop_and_vad_wired_in_realtime_js(self):
        # Section 1/4: continuous listen -> commit -> respond -> listen
        # loop, without requiring a button press for every turn.
        self.assertIn('_resumeListening', self.realtime_js)
        self.assertIn('_startVad', self.realtime_js)
        self.assertIn('_sampleVad', self.realtime_js)
        self.assertIn('VAD_SILENCE_COMMIT_MS', self.realtime_js)
        self.assertIn('VAD_BARGE_IN_MS', self.realtime_js)

    def test_mute_is_distinct_from_exit_in_realtime_js(self):
        # Section 6: mute pauses capture and keeps the session alive; only
        # close() tears down the session/mic/transport.
        self.assertIn('mute()', self.realtime_js)
        self.assertIn('unmute()', self.realtime_js)
        self.assertIn('getAudioTracks().forEach((t) => (t.enabled = false))', self.realtime_js)


class PersonalizationSettingsUiWiringTests(unittest.TestCase):
    def setUp(self):
        self.html = _read("static/panda/index.html")
        self.app_js = _read("static/panda/js/app.js")
        self.pz_js = _read("static/panda/js/personalization.js")

    def test_settings_button_and_dialog_present(self):
        self.assertIn('id="personalization-btn"', self.html)
        self.assertIn('id="personalization-dialog"', self.html)
        for field_id in ("pz-style", "pz-tone", "pz-length", "pz-language", "pz-voice", "pz-voice-preview"):
            self.assertIn(f'id="{field_id}"', self.html)

    def test_all_five_required_styles_present_in_selector(self):
        for value in ("default", "professional", "friendly", "concise", "detailed"):
            self.assertIn(f'value="{value}"', self.html)

    def test_language_options_ru_en_auto_present(self):
        self.assertIn('id="pz-language"', self.html)
        idx = self.html.index('id="pz-language"')
        snippet = self.html[idx : idx + 400]
        self.assertIn('value="auto"', snippet)
        self.assertIn('value="ru"', snippet)
        self.assertIn('value="en"', snippet)

    def test_personalization_js_hits_canonical_rest_endpoints(self):
        self.assertIn('/api/v1/personalization', self.pz_js)
        self.assertIn('/preferences', self.pz_js)
        self.assertIn('/voices', self.pz_js)
        self.assertIn('preview', self.pz_js)

    def test_app_js_wires_settings_button_and_loads_preferred_voice_on_entry(self):
        self.assertIn('els.personalizationBtn.onclick', self.app_js)
        self.assertIn('openPersonalizationSettings', self.app_js)
        self.assertIn('PandaPersonalizationApi.getPreferences', self.app_js)

    def test_voice_change_propagates_to_active_realtime_session_without_new_conversation(self):
        # 4.29.2/4.29.3: selecting a new voice mid-session calls
        # controller.selectVoice, never creates a new conversation/session.
        self.assertIn('selectVoice', self.app_js)
        self.assertNotIn('controller.start(', self.pz_js)


if __name__ == "__main__":
    unittest.main()
