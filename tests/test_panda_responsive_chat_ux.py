"""Deterministic tests — Panda Responsive Chat UX contracts."""

from __future__ import annotations

import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class ResponsiveChatContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("static", "panda", "index.html")
        cls.css = _read("static", "panda", "panda.css")
        cls.theme = _read("static", "shared", "theme.css")
        cls.app = _read("static", "panda", "js", "app.js")
        cls.components = _read("static", "panda", "js", "components.js")
        cls.brand = _read("static", "shared", "brand.js")

    def test_viewport_meta_allows_scaling(self):
        self.assertIn('name="viewport"', self.html)
        self.assertNotIn("user-scalable=no", self.html)
        self.assertNotIn("maximum-scale=1", self.html)
        self.assertIn("viewport-fit=cover", self.html)

    def test_official_logo_canonical(self):
        self.assertIn("/static/panda/assets/panda-logo.png", self.brand)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "static", "panda", "assets", "panda-logo.png")))

    def test_light_theme_default(self):
        self.assertIn('data-theme="light"', self.html)
        self.assertIn("--background:", self.theme)

    def test_shell_uses_fluid_height(self):
        self.assertIn("100dvh", self.theme)
        self.assertIn("--app-height", self.theme)
        self.assertIn("minmax(0, 1fr)", self.theme)

    def test_sidebar_drawer_contract(self):
        self.assertIn("sidebar-backdrop", self.html)
        self.assertIn("sidebar-close", self.html)
        self.assertIn("@media (max-width: 1023px)", self.theme)
        self.assertIn("setSidebarOpen", self.app)
        self.assertIn('Escape', self.app)
        self.assertIn("aria-expanded", self.html)

    def test_composer_bottom_breathing_room(self):
        self.assertIn("composer-dock", self.html)
        self.assertIn("--composer-bottom", self.theme)
        self.assertIn("clamp(", self.theme)
        self.assertIn("safe-area-inset-bottom", self.theme)
        self.assertIn("var(--composer-bottom)", self.css)
        self.assertIn("var(--safe-bottom)", self.css)

    def test_composer_aligns_with_chat_column(self):
        self.assertIn("chat-column", self.html)
        self.assertIn("--chat-max", self.theme)
        self.assertRegex(self.css, r"\.composer-dock[\s\S]*chat-column|\.chat-column")

    def test_messages_wrap_safely(self):
        self.assertIn("overflow-wrap: break-word", self.css)
        # URLs/code may use anywhere; normal message text uses break-word
        self.assertIn(".msg {", self.css)
        self.assertIn("table-scroll", self.components)
        self.assertIn("overflow-x: auto", self.css)
        self.assertNotRegex(
            self.css,
            r"\.msg\s*\{[^}]*overflow-wrap:\s*anywhere",
        )

    def test_no_body_horizontal_scroll_intent(self):
        self.assertIn("overflow: hidden", self.css)
        self.assertIn("overflow-x: hidden", self.css)
        self.assertIn("min-width: 0", self.theme)

    def test_textarea_autogrow_present(self):
        self.assertIn("autoGrowComposer", self.app)
        self.assertIn('resize: none', self.css)
        self.assertIn("min-height: 44px", self.css)

    def test_smart_scroll_preserves_upscroll(self):
        self.assertIn("isNearBottom", self.app)
        self.assertIn("forceScroll", self.app)

    def test_role_nav_still_hidden_by_default(self):
        self.assertIn('id="owner-nav-link"', self.html)
        self.assertRegex(self.html, r'id="owner-nav-link"[^>]*class="[^"]*hidden')

    def test_no_credentials_in_frontend(self):
        blob = "\n".join([self.html, self.css, self.app, self.brand])
        self.assertNotRegex(blob, r"sk-[a-zA-Z0-9]{10,}")
        self.assertNotIn("PANDA_API_KEYS=", blob)

    def test_breakpoint_matrix_present(self):
        for bp in ("1440px", "1023px", "767px", "479px", "640px"):
            self.assertTrue(
                bp in self.css or bp in self.theme,
                f"missing breakpoint coverage for {bp}",
            )

    def test_prefers_reduced_motion(self):
        self.assertIn("prefers-reduced-motion", self.theme)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_existing_static_modules_still_referenced(self):
        for mod in ("sanitize.js", "api-client.js", "components.js", "app.js", "brand.js"):
            self.assertIn(mod, self.html)


if __name__ == "__main__":
    unittest.main()
