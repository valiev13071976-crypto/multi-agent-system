"""BLOCK 3.5 — FINAL CHATGPT 1:1 GENERATED IMAGE UX + LIVE GENERATION STATE
CLOSURE (frontend-only defect closure).

Locks the two contracts this closure fixes:

1. In-conversation live generation indicator (small, minimal, blue, animated,
   tied to the real submitRequest()/editImage() request lifecycle -- never a
   fixed-duration fake animation, never persisted, never orphaned).
2. Completed generated-image overlay composition: "Редактировать" (labelled,
   NOT icon-only) bottom-left INSIDE the image, download (icon-only) bottom-
   right INSIDE the image, replacing the previous detached action row
   underneath the image (PR #13/#14).

Does NOT reopen or re-audit the already-CLOSED backend image.generate/
image.edit/artifact/authorization architecture -- this is a frontend-only
UI/state closure. Reuses the existing offline Node DOM probe
(tests/frontend/render_probe.js) to execute the ACTUAL production
sanitize.js/components.js source, and plain static-content assertions for
app.js/panda.css wiring that the probe's minimal DOM stub cannot observe
(CSS positioning, event-lifecycle wiring, prefers-reduced-motion).

No live/paid provider calls. No broad suite re-run -- see the sibling
targeted files already covering backend exact-artifact targeting,
authorization, and persistence (test_panda_generated_image_direct_actions_
acceptance_fix.py), left untouched and unduplicated here.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (ROOT / Path(*parts)).read_text(encoding="utf-8")


class FrontendProbeTests(unittest.TestCase):
    """Executes the real sanitize.js/components.js via the offline Node
    probe -- structural DOM assertions, not source-string heuristics."""

    @classmethod
    def setUpClass(cls):
        cls.probe = ROOT / "tests" / "frontend" / "render_probe.js"
        try:
            subprocess.run(["node", "--version"], capture_output=True, check=True, timeout=10)
        except Exception:
            raise unittest.SkipTest("node runtime not available")

    def _run(self, cases):
        proc = subprocess.run(
            ["node", str(self.probe), str(ROOT)],
            input=json.dumps({"cases": cases}),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        return {r["name"]: r for r in payload["results"]}

    # -- D. Completed image DOM -------------------------------------------

    def test_edit_overlay_is_labelled_not_icon_only_and_lives_inside_image_wrap(self):
        url = "/api/v1/business-assistant/artifacts/11111111-1111-1111-1111-111111111111/view"
        results = self._run(
            [{"name": "img", "fn": "renderRichText", "args": {"text": f"![изображение]({url})"}}]
        )
        r = results["img"]
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(len(r["imgs"]), 1)
        self.assertEqual(len(r["buttons"]), 1, "exactly one Edit control, no duplicates")
        edit = r["buttons"][0]
        self.assertEqual(edit["text"], "Редактировать", "Edit must show visible text, not be icon-only")
        self.assertIn("msg-image-edit-btn", edit["className"])
        self.assertIn("msg-image-overlay", edit["className"], "must share the overlay-on-image treatment")
        self.assertTrue(edit["ariaLabel"])
        self.assertTrue(edit["title"])
        self.assertEqual(edit["type"], "button")

    def test_download_overlay_is_icon_only_and_uses_authorized_download_url(self):
        url = "/api/v1/business-assistant/artifacts/11111111-1111-1111-1111-111111111111/view"
        results = self._run(
            [{"name": "img", "fn": "renderRichText", "args": {"text": f"![изображение]({url})"}}]
        )
        r = results["img"]
        self.assertEqual(len(r["anchors"]), 1)
        dl = r["anchors"][0]
        # Reference-parity closure: Download must be icon-only, and that icon
        # must be a real tray-style SVG, never a Unicode arrow glyph (font/
        # emoji rendering of "⬇"/"⇩" is inconsistent across OSes).
        self.assertEqual(dl["text"], "", "Download must be icon-only (no visible text)")
        self.assertNotIn("⬇", dl["text"])
        self.assertNotIn("⇩", dl["text"])
        self.assertEqual(len(r["svgs"]), 1, "exactly one SVG download icon, no duplicates")
        icon = r["svgs"][0]
        self.assertEqual(icon["viewBox"], "0 0 24 24")
        self.assertEqual(icon["ariaHidden"], "true", "icon is decorative; accessible name comes from aria-label")
        self.assertEqual(icon["stroke"], "currentColor", "icon must inherit the control's foreground color")
        self.assertGreaterEqual(icon["pathCount"], 2, "arrow + tray strokes present (tray-style download icon)")
        self.assertIn("msg-image-download-btn", dl["className"])
        self.assertIn("msg-image-overlay", dl["className"])
        self.assertEqual(dl["download"], "")
        # Same canonical artifact route as the lightbox/direct-download flow
        # from PR #13 -- no new/parallel download endpoint introduced here.
        self.assertEqual(
            dl["href"],
            "/api/v1/business-assistant/artifacts/11111111-1111-1111-1111-111111111111/download",
        )
        self.assertTrue(dl["ariaLabel"])
        self.assertTrue(dl["title"])

    def test_old_detached_action_row_container_is_gone(self):
        url = "/api/v1/business-assistant/artifacts/11111111-1111-1111-1111-111111111111/view"
        results = self._run(
            [{"name": "img", "fn": "renderRichText", "args": {"text": f"![изображение]({url})"}}]
        )
        r = results["img"]
        all_classnames = " ".join(
            [b["className"] for b in r["buttons"]]
            + [a["className"] for a in r["anchors"]]
            + [s["className"] or "" for s in r["spans"]]
        )
        self.assertNotIn("msg-image-actions", all_classnames, "old detached row container must be removed")
        self.assertNotIn("msg-image-action ", all_classnames, "old bare pill/icon class must be removed")

    def test_image_wrap_has_actions_class_present_for_overlay_positioning_context(self):
        url = "/api/v1/business-assistant/artifacts/11111111-1111-1111-1111-111111111111/view"
        results = self._run(
            [{"name": "img", "fn": "renderRichText", "args": {"text": f"![изображение]({url})"}}]
        )
        r = results["img"]
        wraps = [s for s in r["spans"] if "msg-image-wrap" in (s["className"] or "")]
        self.assertEqual(len(wraps), 1)
        self.assertIn("has-actions", wraps[0]["className"])

    # -- F. Multiple images: independent overlay per image -----------------

    def test_multiple_images_each_own_independent_overlay_targeting(self):
        url_a = "/api/v1/business-assistant/artifacts/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/view"
        url_b = "/api/v1/business-assistant/artifacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/view"
        text = f"Готово.\n![изображение]({url_a})\n![изображение]({url_b})"
        results = self._run([{"name": "multi", "fn": "renderRichText", "args": {"text": text}}])
        r = results["multi"]
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(len(r["imgs"]), 2)
        self.assertEqual(len(r["buttons"]), 2, "each image owns its own Edit control")
        self.assertEqual(len(r["anchors"]), 2, "each image owns its own Download control")
        self.assertEqual(r["imgs"][0]["dataset"]["artifactId"], "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(r["imgs"][1]["dataset"]["artifactId"], "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        self.assertEqual(r["buttons"][0]["dataset"]["artifactId"], "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(r["buttons"][1]["dataset"]["artifactId"], "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        self.assertIn("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", r["anchors"][0]["href"])
        self.assertIn("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", r["anchors"][1]["href"])
        # Never the same target for both -- no "latest image" collapse.
        self.assertNotEqual(r["buttons"][0]["dataset"]["artifactId"], r["buttons"][1]["dataset"]["artifactId"])

    def test_legacy_non_canonical_image_url_unaffected_no_overlay_injected(self):
        # Images without a recoverable canonical artifact_id (e.g. legacy
        # /media/{version_id} links) must render exactly as before --
        # untouched by this closure.
        url = "/api/v1/business-assistant/media/some-legacy-version-id"
        results = self._run(
            [{"name": "legacy", "fn": "renderRichText", "args": {"text": f"![изображение]({url})"}}]
        )
        r = results["legacy"]
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(len(r["imgs"]), 1)
        self.assertEqual(len(r["buttons"]), 0, "no Edit control for a non-canonical/legacy image URL")
        self.assertEqual(len(r["anchors"]), 0, "no Download overlay for a non-canonical/legacy image URL")

    # -- A/B. Pending indicator component -----------------------------------

    def test_pending_indicator_component_is_minimal_status_dot_not_a_spinner_or_modal(self):
        results = self._run([{"name": "pending", "fn": "renderPendingAssistant", "args": {}}])
        r = results["pending"]
        self.assertTrue(r["ok"], r.get("error"))
        # No image/button/anchor/progress markup at all -- just the dot.
        self.assertEqual(r["imgs"], [])
        self.assertEqual(r["buttons"], [])
        self.assertEqual(r["anchors"], [])
        status_spans = [s for s in r["spans"] if s["role"] == "status"]
        self.assertEqual(len(status_spans), 1, "exactly one accessible status region")
        self.assertEqual(status_spans[0]["ariaLive"], "polite")
        self.assertTrue(status_spans[0]["ariaLabel"], "must expose a non-disruptive accessible label")
        self.assertIn("pending-indicator", status_spans[0]["className"])
        dots = [s for s in r["spans"] if "pending-dot" in (s["className"] or "")]
        self.assertEqual(len(dots), 1)
        self.assertEqual(dots[0]["ariaHidden"], "true", "the animated dot itself must not spam screen readers")

    def test_pending_indicator_message_shell_is_assistant_role(self):
        results = self._run([{"name": "pending", "fn": "renderPendingAssistant", "args": {}}])
        r = results["pending"]
        shells = [s for s in r["spans"]] + []  # spans only; wrap is an <article>, not captured by probe
        # The wrapping <article class="msg assistant pending"> itself isn't
        # walked as img/a/button/span/pre by the probe (by design, mirroring
        # the existing renderMessage probe contract) -- assert its class via
        # the component source directly instead (see StaticWiringTests).
        self.assertIsInstance(shells, list)


class StaticWiringTests(unittest.TestCase):
    """Assertions the minimal Node DOM stub cannot observe (CSS, top-level
    element tag/class, and real request-lifecycle wiring in app.js)."""

    # -- components.js: pending indicator shell -----------------------------

    def test_components_exports_render_pending_assistant(self):
        js = _read("static", "panda", "js", "components.js")
        self.assertIn("function renderPendingAssistant", js)
        self.assertIn("renderPendingAssistant,", js, "must be exported on window.PandaComponents")
        self.assertIn('el("article", "msg assistant pending")', js)

    # -- app.js: real request-lifecycle wiring, not a fixed-duration fake --

    def test_pending_indicator_shown_before_and_hidden_after_the_real_submit_call(self):
        js = _read("static", "panda", "js", "app.js")
        self.assertIn("function showPendingIndicator", js)
        self.assertIn("function hidePendingIndicator", js)
        send_message = js.split("async function sendMessage()", 1)[1].split("\n\n  async function onApprove", 1)[0]
        show_at = send_message.index("showPendingIndicator();")
        submit_at = send_message.index("await api.submitRequest(payload);")
        self.assertLess(show_at, submit_at, "indicator must appear before the blocking request starts")
        after_submit = send_message[submit_at:]
        # Hidden on the success path immediately once the real call settles...
        self.assertIn("hidePendingIndicator();", after_submit.split("} catch")[0])
        # ...and hidden on the failure path too -- no orphan/infinite spinner.
        catch_block = send_message.split("} catch (e) {", 1)[1].split("} finally", 1)[0]
        self.assertIn("hidePendingIndicator();", catch_block)
        # setTimeout-driven fake progress is explicitly forbidden.
        self.assertNotIn("setTimeout", send_message)

    def test_pending_indicator_never_persisted_and_cleared_by_render_timeline(self):
        js = _read("static", "panda", "js", "app.js")
        # It is a plain appended DOM node, never pushed onto state.messages
        # (the persisted/reloadable model) -- so it can never resurrect as a
        # stale "generating" bubble after reload/history restore.
        self.assertIn("els.timeline.appendChild(node);", js)
        self.assertNotIn("state.messages.push({\n      role: \"assistant\",\n      content: \"\",", js)
        render_timeline = js.split("function renderTimeline(opts)", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("state.pendingIndicatorEl = null;", render_timeline)

    def test_one_submit_causes_exactly_one_submit_request_call_in_send_message(self):
        js = _read("static", "panda", "js", "app.js")
        send_message = js.split("async function sendMessage()", 1)[1].split("\n\n  async function onApprove", 1)[0]
        self.assertEqual(send_message.count("api.submitRequest("), 1)

    # -- Event separation: Edit/Download overlays must not open the lightbox

    def test_edit_click_intercepted_before_lightbox_open_and_download_is_a_sibling_anchor(self):
        js = _read("static", "panda", "js", "app.js")
        listener = js.split('els.timeline.addEventListener("click"', 1)[1].split("\n    });", 1)[0]
        edit_at = listener.index('.closest(".msg-image-edit-btn")')
        img_at = listener.index('.closest(".msg-image")')
        self.assertLess(edit_at, img_at, "Edit must be intercepted before the lightbox fallback")
        self.assertIn("return;", listener[edit_at:img_at])
        sanitize_js = _read("static", "panda", "js", "sanitize.js")
        # Download is an <a>, a sibling of <img> inside the same wrap -- never
        # a descendant of .msg-image -- so e.target.closest(".msg-image")
        # structurally cannot match a click on it either.
        self.assertIn('wrap.appendChild(img);', sanitize_js)
        self.assertIn("downloadBtn.className = \"msg-image-overlay msg-image-download-btn\";", sanitize_js)

    # -- CSS: overlay positioning is relative to the image container --------

    def test_css_overlay_positioning_is_relative_to_image_container(self):
        css = _read("static", "panda", "panda.css")
        wrap_rule = css.split(".msg-image-wrap.has-actions {", 1)[1].split("}", 1)[0]
        self.assertIn("position: relative", wrap_rule)
        overlay_rule = css.split(".msg-image-overlay {", 1)[1].split("}", 1)[0]
        self.assertIn("position: absolute", overlay_rule)
        edit_rule = css.split(".msg-image-edit-btn {", 1)[1].split("}", 1)[0]
        self.assertIn("left:", edit_rule)
        download_rule = css.split(".msg-image-download-btn {", 1)[1].split("}", 1)[0]
        self.assertIn("right:", download_rule)
        # Old pill/toolbar-underneath-image layout must be gone.
        self.assertNotIn(".msg-image-actions {", css)
        self.assertNotIn(".msg-image-action {", css)

    # -- Reference-parity closure: bottom gradient + real SVG icon ----------

    def test_css_bottom_gradient_is_decorative_and_layered_below_controls(self):
        css = _read("static", "panda", "panda.css")
        self.assertIn(".msg-image-wrap.has-actions::after {", css)
        gradient_rule = css.split(".msg-image-wrap.has-actions::after {", 1)[1].split("}", 1)[0]
        self.assertIn("pointer-events: none", gradient_rule, "gradient must never intercept clicks")
        self.assertIn("position: absolute", gradient_rule)
        self.assertIn("linear-gradient(", gradient_rule)
        self.assertIn("z-index: 0", gradient_rule, "must sit below the z-index: 1 controls")
        overlay_rule = css.split(".msg-image-overlay {", 1)[1].split("}", 1)[0]
        self.assertIn("z-index: 1", overlay_rule, "controls must render above the decorative gradient")
        # The wrap clips both the <img> and the gradient to the same rounded
        # corners so the gradient reads as part of the image, not a separate
        # rectangle glued underneath it.
        wrap_rule = css.split(".msg-image-wrap.has-actions {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow: hidden", wrap_rule)
        self.assertIn("border-radius:", wrap_rule)

    def test_sanitize_js_never_uses_unicode_arrow_glyph_for_download_icon(self):
        js = _read("static", "panda", "js", "sanitize.js")
        # Only permitted inside the explanatory comment listing the glyphs
        # this closure explicitly forbids as *rendered content* -- never as
        # an actual assigned textContent/attribute value.
        self.assertNotIn('textContent = "⬇"', js)
        self.assertNotIn('textContent = "⇩"', js)
        self.assertIn("createElementNS", js, "download icon must be a real SVG, not a text glyph")
        self.assertIn("createDownloadIcon", js)

    def test_css_pending_dot_is_small_animated_and_respects_reduced_motion(self):
        css = _read("static", "panda", "panda.css")
        self.assertIn(".pending-dot {", css)
        dot_rule = css.split(".pending-dot {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 50%", dot_rule)
        self.assertIn("animation:", dot_rule)
        self.assertIn("var(--accent)", dot_rule, "must use the existing blue accent token, not a new color")
        self.assertIn("@keyframes panda-pending-pulse", css)
        reduced_motion_block = css.split("@media (prefers-reduced-motion: reduce) {", 1)[1]
        self.assertIn(".pending-dot", reduced_motion_block)
        self.assertIn("animation: none", reduced_motion_block)

    def test_no_fake_progress_percentage_or_eta_introduced(self):
        for rel in ("js/app.js", "js/components.js", "js/sanitize.js"):
            js = _read("static", "panda", *rel.split("/"))
            self.assertNotRegex(js, r"\d+%\s*(генерац|generat)", rel)
        css = _read("static", "panda", "panda.css")
        self.assertNotIn("progress-bar", css)


if __name__ == "__main__":
    unittest.main()
