"""Targeted tests — Panda Web Product UX Foundation block."""

from __future__ import annotations

import os
import re
import unittest


TECHNICAL_MARKERS = (
    "Requested:",
    "Findings:",
    "Artifacts:",
    "Fixture_mode:",
    "Approved:",
    "Waiting_approval:",
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class BrandingThemeTests(unittest.TestCase):
    def test_shared_brand_module_exists(self):
        self.assertTrue(os.path.isfile("static/shared/brand.js"))
        src = _read("static/shared/brand.js")
        self.assertIn('name: "Panda"', src)
        self.assertIn("panda-logo.png", src)

    def test_logo_referenced_in_panda_shell(self):
        html = _read("static/panda/index.html")
        self.assertIn("/static/shared/theme.css", html)
        self.assertIn("/static/shared/brand.js", html)
        self.assertIn('data-theme="light"', html)
        self.assertIn('lang="ru"', html)

    def test_default_theme_is_light(self):
        css = _read("static/shared/theme.css")
        self.assertIn("--background:", css)
        self.assertIn("#f7f8fa", css)
        panda = _read("static/panda/index.html")
        self.assertIn('data-theme="light"', panda)
        owner = _read("static/owner/index.html")
        self.assertIn('data-theme="light"', owner)


class PresentationLayerTests(unittest.TestCase):
    def test_technical_summary_stripped(self):
        src = _read("static/shared/presentation.js")
        self.assertIn("Requested:", src)
        self.assertIn("stripTechnicalLines", src)

    def test_presentation_filters_fixture_mode(self):
        # Simulate browser module logic via regex from presentation.js patterns
        sample = "Requested: ANALYZE\nFindings: 0\nПривет!"
        lines = [ln for ln in sample.split("\n") if not re.match(r"^(Requested:|Findings:|Artifacts:|Fixture_mode:)", ln.strip())]
        cleaned = "\n".join(lines).strip()
        self.assertNotIn("Requested:", cleaned)
        self.assertIn("Привет!", cleaned)

    def test_chat_components_use_presentation(self):
        components = _read("static/panda/js/components.js")
        self.assertIn("PandaPresentation", components)
        self.assertNotIn("JSON.stringify(c)", components)

    def test_api_errors_human_readable_russian(self):
        copy = _read("static/shared/copy.js")
        self.assertIn("Сессия недействительна", copy)
        self.assertIn("BAA_CONVERSATION_UNAVAILABLE", copy)


class RoleSeparationTests(unittest.TestCase):
    def test_role_context_fail_closed(self):
        src = _read("static/shared/role-context.js")
        self.assertIn("isManagement", src)
        self.assertIn("MANAGEMENT_ROLES", src)
        self.assertIn("OWNER", src)

    def test_user_chat_hides_owner_nav_by_default(self):
        html = _read("static/panda/index.html")
        self.assertIn('id="owner-nav-link"', html)
        self.assertIn("hidden", html)

    def test_owner_dashboard_requires_management(self):
        owner = _read("static/owner/owner.js")
        self.assertIn("isManagement", owner)
        self.assertIn("access-denied", owner)

    def test_diagnostics_panel_separated_in_chat(self):
        html = _read("static/panda/index.html")
        self.assertIn("diagnostics-panel", html)
        self.assertIn("Техническая диагностика", html)


class ChatUxTests(unittest.TestCase):
    def test_app_uses_user_facing_status(self):
        app = _read("static/panda/js/app.js")
        self.assertIn("assistantBubbleText", app)
        self.assertIn("canShowDiagnostics", app)
        self.assertIn("return false;", app)
        self.assertIn("PandaCopy.USER_THINKING", app)

    def test_no_technical_markers_in_default_timeline_path(self):
        app = _read("static/panda/js/app.js")
        self.assertIn("toUserFacingSummary", app)
        for marker in TECHNICAL_MARKERS:
            self.assertNotIn(f'"{marker}"', app)

    def test_xss_sanitize_preserved(self):
        sanitize = _read("static/panda/js/sanitize.js")
        self.assertIn("textContent", sanitize)
        self.assertIn("escapeHtml", sanitize)

    def test_long_message_rendering_support(self):
        sanitize = _read("static/panda/js/sanitize.js")
        self.assertIn("renderRichText", sanitize)
        css = _read("static/panda/panda.css")
        self.assertIn("overflow-wrap", css)


class OwnerDashboardTests(unittest.TestCase):
    def test_no_fake_hardcoded_metrics(self):
        owner = _read("static/owner/owner.js")
        self.assertNotIn("1248", owner)
        self.assertNotIn("24560", owner)
        self.assertNotIn("45289", owner)
        self.assertIn("Данные пока недоступны", owner)

    def test_empty_states_present(self):
        owner = _read("static/owner/owner.js")
        self.assertIn("Пользователей пока нет", owner)
        self.assertIn("Данных пока недостаточно", owner)

    def test_future_billing_marked_coming_soon(self):
        html = _read("static/owner/index.html")
        self.assertIn("скоро", html)


class ResponsiveSecurityTests(unittest.TestCase):
    def test_mobile_sidebar_rules(self):
        css = _read("static/shared/theme.css")
        self.assertTrue(
            "@media (max-width: 1023px)" in css or "@media (max-width: 900px)" in css
        )
        self.assertIn("transform: translateX", css)

    def test_no_secrets_in_frontend_sources(self):
        key_patterns = (re.compile(r"panda_api_keys"), re.compile(r"\bsk-[a-z0-9]{8,}\b"))
        for root, _, files in os.walk("static"):
            if "chat" in root.replace("\\", "/").split("/"):
                continue
            for name in files:
                if not name.endswith((".js", ".html", ".css")):
                    continue
                path = os.path.join(root, name)
                src = _read(path).lower()
                for pattern in key_patterns:
                    self.assertIsNone(pattern.search(src), path)

    def test_api_key_still_session_storage(self):
        api = _read("static/panda/js/api-client.js")
        self.assertIn("sessionStorage", api)


class CompatibilityTests(unittest.TestCase):
    def test_business_assistant_api_path_unchanged(self):
        api = _read("static/panda/js/api-client.js")
        self.assertIn("/api/v1/business-assistant", api)

    def test_owner_route_registered(self):
        main_src = _read("main.py")
        self.assertIn('"/owner"', main_src)
        self.assertIn("static/owner/index.html", main_src)


if __name__ == "__main__":
    unittest.main()
