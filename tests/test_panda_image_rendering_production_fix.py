"""PANDA GENERATED IMAGE ARTIFACT / RENDERING PRODUCTION FIX.

Deterministic, offline-safe regression tests proving/disproving the reported
production defect ("Panda web chat renders large solid blue rectangles
instead of the generated image").

Does NOT reopen or modify the CLOSED "PANDA ACTION EXECUTION & MULTI-TURN
TOOL CONTINUATION" or "PANDA LIVE IMAGE GENERATION ACTIVATION" blocks. Adds a
new, standalone regression file only.

No real provider/API/paid/network calls. The OpenAI adapter boundary test
below stubs its internal bounded HTTP client instead of calling the network.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from PIL import Image

from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from integrations.production.adapters.media import build_image_provider
from product_media.providers.fake import FakeImageGenerationProvider, ProviderResult
from product_media.runtime import build_product_media_runtime
from product_media.tools import ProductMediaToolAdapter
from side_effects.runtime import compose_side_effect_runtime
from tools.models import ToolRequest

ROOT = Path(__file__).resolve().parent.parent
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _distinctive_fixture_png() -> bytes:
    """Deterministic, non-blue, non-solid fixture — a small red/green
    checkerboard. Deliberately NOT reusing FakeImageGenerationProvider's
    output (which is itself solid blue) so the byte-fidelity assertions
    below cannot be satisfied by accident if some layer swapped in a
    different placeholder."""
    img = Image.new("RGB", (64, 64), color=(0, 0, 0))
    for y in range(0, 64, 8):
        for x in range(0, 64, 8):
            color = (220, 20, 20) if ((x // 8) + (y // 8)) % 2 == 0 else (20, 200, 20)
            for py in range(y, y + 8):
                for px in range(x, x + 8):
                    img.putpixel((px, py), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


FIXTURE_PNG_BYTES = _distinctive_fixture_png()


class FixtureImageProvider:
    """Test-only deterministic provider returning the distinctive fixture,
    standing in for whatever real/offline generator is wired in production."""

    provider_id = "test-fixture"
    calls = 0

    def generate(self, *, prompt: str, width: int = 512, height: int = 512, seed=None) -> ProviderResult:
        self.calls += 1
        return ProviderResult(
            data=FIXTURE_PNG_BYTES,
            mime_type="image/png",
            provider_id=self.provider_id,
            profile_version="1.0.0",
        )


class RootCauseInvestigationTests(unittest.TestCase):
    """Boundary A/root-cause: what does the ACTIVE (offline, no-key) provider
    really return, and does the real-provider adapter normalize correctly?"""

    def test_fake_provider_output_matches_reported_blue_rectangle_signature(self):
        """Documents the exact root cause: the offline FakeImageGenerationProvider
        (the provider silently active whenever MEDIA_IMAGE_PROVIDER/API key are
        not configured, which is the current Railway production state) paints a
        512x512 canvas that is >99% one solid blue RGB value with a near-invisible
        text label — this IS what a user sees as 'a large solid blue rectangle'.
        This is legitimate, valid, correctly-generated placeholder image content,
        not a corrupted/misrendered artifact."""
        provider = FakeImageGenerationProvider()
        result = provider.generate(prompt="панда есть бамбук")
        self.assertTrue(result.data.startswith(PNG_SIGNATURE))
        self.assertEqual(result.mime_type, "image/png")
        img = Image.open(io.BytesIO(result.data))
        colors = img.getcolors(maxcolors=1_000_000)
        total = img.width * img.height
        dominant_count, dominant_rgb = max(colors, key=lambda c: c[0])
        self.assertEqual(dominant_rgb, (40, 120, 200), "fake provider's fixed placeholder color")
        self.assertGreater(dominant_count / total, 0.99, "placeholder is >99% one solid color")

    def test_production_provider_selection_silently_defaults_to_fake_without_credentials(self):
        """Documents (does not 'fix' — out of scope, no real/paid calls allowed
        in this block) that build_image_provider silently returns the fake
        placeholder generator whenever MEDIA_IMAGE_PROVIDER/API key env vars are
        absent, REGARDLESS of a production-looking environment. This is the
        exact condition that explains the deployed behavior."""
        self.assertIsInstance(build_image_provider({}), FakeImageGenerationProvider)
        self.assertIsInstance(
            build_image_provider({"PANDA_ENV": "production"}), FakeImageGenerationProvider
        )

    def test_real_provider_adapter_normalizes_base64_response_to_raw_png_bytes(self):
        """Boundary: if/when a real provider is configured, verify the existing
        adapter correctly normalizes its response shape (base64 JSON) into raw
        binary + a known image mime type at the provider boundary -- not left
        for the browser to interpret. No network call is made; the bounded HTTP
        client is stubbed with a canned response."""
        import base64

        from integrations.production.adapters.media import OpenAIImageGenerationProvider

        canned_b64 = base64.b64encode(FIXTURE_PNG_BYTES).decode("ascii")

        provider = OpenAIImageGenerationProvider(api_key="test-key-not-real")

        class _StubResp:
            def json(self):
                return {"data": [{"b64_json": canned_b64}]}

        provider._http = Mock()
        provider._http.request = Mock(return_value=_StubResp())

        result = provider.generate(prompt="панда есть бамбук")
        self.assertEqual(result.data, FIXTURE_PNG_BYTES)
        self.assertEqual(result.mime_type, "image/png")
        provider._http.request.assert_called_once()


class PipelineByteFidelityTests(unittest.IsolatedAsyncioTestCase):
    """CRITICAL VALID IMAGE TEST: the persisted + retrieved artifact must be
    byte-identical to a distinctive, non-blue fixture -- not a placeholder,
    not JSON, not the wrong asset."""

    async def test_generate_persists_exact_bytes_not_a_placeholder(self):
        service = build_product_media_runtime(db_path=":memory:")
        service.generator = FixtureImageProvider()
        adapter = ProductMediaToolAdapter(service)
        request = ToolRequest(
            request_id="r1",
            workflow_id="",
            task_id="t1",
            tool_id="image.generate",
            operation="generate",
            arguments={"scene_description": "панда есть бамбук", "variant_count": 1},
            tenant_id="tenant-a",
        )
        data = await adapter.execute_read(request, {})
        version_id = data["version_ids"][0]

        blob = service.get_blob(tenant_id="tenant-a", version_id=version_id)
        self.assertTrue(blob.startswith(PNG_SIGNATURE), "must be a valid PNG (magic bytes present)")
        self.assertEqual(blob, FIXTURE_PNG_BYTES, "served bytes must exactly match the generated image")
        decoded = Image.open(io.BytesIO(blob))
        self.assertEqual(decoded.format, "PNG")
        self.assertEqual(decoded.size, (64, 64))

        version = service.get(tenant_id="tenant-a", version_id=version_id)
        self.assertEqual(version.mime_type, "image/png")
        self.assertEqual(data["mime_type"], "image/png")
        self.assertTrue(data["view_url"].startswith("/api/v1/business-assistant/media/"))
        self.assertIn(version_id, data["view_url"])


class EndToEndOfflineRegressionTests(unittest.IsolatedAsyncioTestCase):
    """REQUIRED END-TO-END OFFLINE TEST: fake deterministic provider ->
    ProductMediaToolAdapter -> real ToolGateway -> Conversation Gateway ->
    "сгенерируй картинку панды, которая ест бамбук" -> persisted artifact ->
    authorized media endpoint semantics, using the real production wiring
    (compose_side_effect_runtime), with the shared fake provider replaced by
    a distinctive fixture provider so byte-substitution bugs cannot hide."""

    async def test_wolf_style_image_request_delivers_exact_fixture_bytes(self):
        runtime = compose_side_effect_runtime(env={})
        fixture_provider = FixtureImageProvider()
        runtime.product_media_service.generator = fixture_provider

        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "unused", "role": "Judge"})
        engine.last_workflow_id = "wf-1"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
        )
        result = await gw.respond(
            ConversationRequest(
                text="сгенерируй картинку панды, которая ест бамбук",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-panda-1",
                conversation_id="c1",
            )
        )
        engine.execute.assert_not_called()
        self.assertEqual(fixture_provider.calls, 1, "provider must be invoked exactly once")

        artifacts = result.metadata.get("artifacts") or []
        self.assertTrue(artifacts, "conversation response must carry frontend-compatible artifact metadata")
        artifact = artifacts[0]
        view_url = str(artifact.get("view_url") or "")
        self.assertTrue(view_url.startswith("/api/v1/business-assistant/media/"))
        self.assertIn(f"![", result.text)
        self.assertIn(view_url, result.text)

        version_id = view_url.rsplit("/", 1)[-1]
        blob = runtime.product_media_service.get_blob(tenant_id="tenant-a", version_id=version_id)
        self.assertEqual(blob, FIXTURE_PNG_BYTES)
        self.assertTrue(blob.startswith(PNG_SIGNATURE))

    async def test_duplicate_request_id_calls_provider_at_most_once(self):
        runtime = compose_side_effect_runtime(env={})
        fixture_provider = FixtureImageProvider()
        runtime.product_media_service.generator = fixture_provider
        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "unused", "role": "Judge"})
        engine.last_workflow_id = "wf-1"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
        )
        req = ConversationRequest(
            text="сгенерируй картинку панды, которая ест бамбук",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="req-panda-dup",
            conversation_id="c1",
        )
        await gw.respond(req)
        await gw.respond(req)
        self.assertEqual(fixture_provider.calls, 1, "idempotency must prevent a second provider invocation")


class MediaEndpointByteFidelityAndIsolationTests(unittest.TestCase):
    """Media endpoint boundary D/E: HTTP GET returns the exact binary image
    bytes with an image/* Content-Type, preserving existing auth and tenant
    isolation (reused, not reimplemented)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["BA_API_DB_PATH"] = os.path.join(cls.tmp, "ba_media.sqlite")
        os.environ["BA_API_UPLOAD_DIR"] = os.path.join(cls.tmp, "uploads")
        os.makedirs(os.environ["BA_API_UPLOAD_DIR"], exist_ok=True)
        os.environ["SECURITY_AUTH_MODE"] = "required"
        os.environ["PANDA_API_KEYS"] = (
            "key-a|tenant-a|user-a|user|secret-render-a;"
            "key-b|tenant-b|user-b|user|secret-render-b"
        )
        os.environ["PRODUCT_MEDIA_DB_PATH"] = ":memory:"

        import importlib

        import main as main_mod

        cls.main = importlib.reload(main_mod)
        cls.client = TestClient(cls.main.app)
        cls.media_service = cls.main.side_effect_runtime.product_media_service
        cls.media_service.generator = FixtureImageProvider()
        result = cls.media_service.generate_from_brief(
            tenant_id="tenant-a", scene_description="панда есть бамбук", variant_count=1
        )
        cls.version_id = result["version_ids"][0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_response_is_exact_valid_image_bytes_with_image_mime(self):
        r = self.client.get(
            f"/api/v1/business-assistant/media/{self.version_id}",
            headers={"X-API-Key": "secret-render-a"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("image/"))
        self.assertEqual(r.content, FIXTURE_PNG_BYTES)
        self.assertTrue(r.content.startswith(PNG_SIGNATURE))
        Image.open(io.BytesIO(r.content)).verify()

    def test_unauthenticated_denied(self):
        r = self.client.get(f"/api/v1/business-assistant/media/{self.version_id}")
        self.assertEqual(r.status_code, 401)

    def test_cross_tenant_denied_fails_closed(self):
        r = self.client.get(
            f"/api/v1/business-assistant/media/{self.version_id}",
            headers={"X-API-Key": "secret-render-b"},
        )
        self.assertEqual(r.status_code, 404)

    def test_unknown_version_returns_404(self):
        r = self.client.get(
            "/api/v1/business-assistant/media/does-not-exist",
            headers={"X-API-Key": "secret-render-a"},
        )
        self.assertEqual(r.status_code, 404)


class FrontendRenderingRegressionTests(unittest.TestCase):
    """Boundary F: execute the ACTUAL production sanitize.js/components.js
    source (unmodified) inside a minimal offline DOM stub via Node.js, and
    verify structural rendering — not a source-string heuristic."""

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

    def test_image_markdown_renders_single_img_with_correct_src(self):
        version_id = str(uuid.uuid4())
        url = f"/api/v1/business-assistant/media/{version_id}"
        results = self._run(
            [
                {
                    "name": "chat_message",
                    "fn": "renderMessage",
                    "args": {
                        "role": "assistant",
                        "content": f"Готово.\n![изображение]({url})",
                        "meta": None,
                    },
                }
            ]
        )
        r = results["chat_message"]
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(len(r["imgs"]), 1, "exactly one <img> — no duplicate rendering")
        self.assertEqual(r["imgs"][0]["src"], url)
        self.assertEqual(r["imgs"][0]["alt"], "изображение")
        self.assertIn("Готово.", r["texts"])

    def test_absolute_railway_url_also_renders_as_img(self):
        url = "https://multi-agent-system-production-8d0c.up.railway.app/api/v1/business-assistant/media/abc"
        results = self._run(
            [{"name": "abs", "fn": "renderRichText", "args": {"text": f"![изображение]({url})"}}]
        )
        r = results["abs"]
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(len(r["imgs"]), 1)
        self.assertEqual(r["imgs"][0]["src"], url)

    def test_javascript_scheme_is_never_rendered_as_img_src(self):
        results = self._run(
            [{"name": "js", "fn": "renderRichText", "args": {"text": "![x](javascript:alert(1))"}}]
        )
        r = results["js"]
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["imgs"], [], "javascript: scheme must never become an <img src>")

    def test_arbitrary_data_scheme_is_never_rendered_as_img_src(self):
        results = self._run(
            [
                {
                    "name": "data",
                    "fn": "renderRichText",
                    "args": {"text": "![x](data:text/html,<script>alert(1)</script>)"},
                }
            ]
        )
        r = results["data"]
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["imgs"], [], "arbitrary data: scheme must never become an <img src>")

    def test_artifact_panel_renderer_also_produces_correct_img(self):
        version_id = str(uuid.uuid4())
        url = f"/api/v1/business-assistant/media/{version_id}"
        results = self._run(
            [
                {
                    "name": "panel",
                    "fn": "renderArtifacts",
                    "args": {
                        "artifacts": [
                            {"artifact_type": "image", "mime_type": "image/png", "view_url": url}
                        ]
                    },
                }
            ]
        )
        r = results["panel"]
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(len(r["imgs"]), 1)
        self.assertEqual(r["imgs"][0]["src"], url)


if __name__ == "__main__":
    unittest.main()
