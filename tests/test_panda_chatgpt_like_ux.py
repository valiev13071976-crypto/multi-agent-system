"""Focused tests — ChatGPT-like responsive Panda /app UX (frontend only)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


class ChatUxStructureTests(unittest.TestCase):
    def test_desktop_shell_and_welcome(self):
        html = _read("static/panda/index.html")
        css = _read("static/shared/theme.css")
        self.assertIn('id="app"', html)
        self.assertIn('id="sidebar"', html)
        self.assertIn('id="welcome-state"', html)
        self.assertIn("Panda AI", html)
        self.assertNotIn("ChatGPT", html)
        self.assertNotIn("OpenAI", html)
        self.assertIn("100dvh", css)
        self.assertIn("sidebar-collapsed", css)
        self.assertIn("grid-template-columns", css)

    def test_mobile_drawer_structure(self):
        html = _read("static/panda/index.html")
        theme = _read("static/shared/theme.css")
        panda = _read("static/panda/panda.css")
        self.assertIn('id="sidebar-toggle"', html)
        self.assertIn('aria-controls="sidebar"', html)
        self.assertIn('id="sidebar-backdrop"', html)
        self.assertIn('id="sidebar-close"', html)
        self.assertIn("@media (max-width: 1023px)", theme)
        self.assertIn("transform: translateX", theme)
        self.assertIn("safe-area-inset-bottom", panda)
        self.assertIn("safe-area-inset-bottom", theme)
        self.assertIn("interactive-widget=resizes-content", html)

    def test_composer_is_integrated_textarea(self):
        html = _read("static/panda/index.html")
        css = _read("static/panda/panda.css")
        js = _read("static/panda/js/app.js")
        self.assertIn('id="composer-input"', html)
        self.assertIn("<textarea", html)
        self.assertIn("composer-send", html)
        self.assertIn('aria-label="Отправить"', html)
        self.assertIn("composer-tool", html)
        self.assertNotIn('class="primary send-btn"', html)
        self.assertIn("flex-wrap: nowrap", css)
        self.assertNotIn("grid-column: 2", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("isComposing", js)
        self.assertIn('e.key === "Enter" && !e.shiftKey', js)
        self.assertIn("composerHasContent", js)
        self.assertIn("updateSendEnabled", js)

    def test_empty_send_and_duplicate_guard(self):
        js = _read("static/panda/js/app.js")
        self.assertIn("if (!text && !state.attachments.length) return", js)
        self.assertIn("if (state.submitting) return", js)
        self.assertIn("setComposerBusy", js)

    def test_conversation_and_logout_preserved(self):
        js = _read("static/panda/js/app.js")
        html = _read("static/panda/index.html")
        self.assertIn("createConversation", js)
        self.assertIn("openConversation", js)
        self.assertIn("closeSidebar()", js)
        self.assertIn("/api/accounts/logout", js)
        self.assertIn("X-CSRF-Token", js)
        self.assertIn("hasHumanSession", js)
        self.assertIn('id="owner-nav-link"', html)
        self.assertIn("nav-item-secondary", html)

    def test_session_and_api_client_unchanged(self):
        js = _read("static/panda/js/app.js")
        api = _read("static/panda/js/api-client.js")
        self.assertIn("/api/v1/business-assistant", api)
        self.assertIn("hasHumanSession", api)
        self.assertIn("panda_api_key", api)
        self.assertNotIn("localStorage.setItem", api)
        self.assertIn("listConversations", js)


class ChatUxAuthBoundaryTests(unittest.TestCase):
    def test_unauthenticated_ba_still_denied_contract(self):
        path = "tests/test_login_session_app_wiring.py"
        self.assertTrue(os.path.isfile(path))
        src = _read(path)
        self.assertIn("test_unauthenticated_ba_still_denied", src)
        self.assertIn("test_json_login_then_ba_conversations_without_api_key", src)
        self.assertIn("test_api_key_still_lists_conversations", src)
