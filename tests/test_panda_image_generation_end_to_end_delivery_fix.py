"""PANDA LIVE IMAGE GENERATION -- FIX PRODUCTION END-TO-END, NO LOOPING.

Follow-up investigation after the model-aware OpenAI request-contract fix
(tests/test_panda_image_generation_production_failure_root_cause.py) was
merged/deployed: production STILL failed every real image.generate request.
This file proves and fixes the actual remaining defects, found by tracing
the complete pipeline stage by stage rather than re-litigating the already-
closed request-contract fix.

DEFECT 1 (primary, most likely current production root cause):
    BoundedHttpClient defaults max_response_bytes to 1 MiB -- sized for
    typical small JSON API responses across OTHER production providers, not
    for a b64_json image response. A real 1024x1024 (or larger) generated
    image commonly decodes to several hundred KB to a few MB of raw PNG
    bytes; base64 inflates that by ~4/3 plus JSON wrapping overhead. Any
    real image whose raw bytes exceed ~750 KB trips this cap, and the
    genuinely successful OpenAI response is discarded as
    ProductionProviderError(INVALID_RESPONSE, "response_too_large") --
    indistinguishable, from the outside, from a real provider failure.
    The prior fix made gpt-image-1/dall-e-3 requests pass OpenAI's request
    validation (no more HTTP 400), which means those models' now-valid,
    larger real responses are MORE likely to hit this previously-untested
    ceiling -- explaining why the failure persisted after that fix shipped.

DEFECT 2 (Railway volume compatibility):
    SqliteMediaStore(path) called sqlite3.connect(path, ...) directly. If
    PRODUCT_MEDIA_DB_PATH points at a path whose parent directory does not
    yet exist (a common Railway-volume-mount gotcha -- the volume exists,
    but the app must create its own subpath), sqlite3.connect() raises
    OperationalError at startup. build_product_media_runtime() call site in
    side_effects/runtime.py silently swallows this as `except Exception:
    product_media_runtime = None`, which disables the ENTIRE product-media
    subsystem -- every image.generate call then fails with
    ToolNotFoundError("tool_unavailable"), surfacing as the exact same
    generic "Не получилось выполнить действие." the user sees.

DEFECT 3 (frontend/text duplication, found while auditing artifact
    delivery): format_tool_user_text() built its image-link list from BOTH
    the top-level "view_url"/"url" convenience field AND the per-asset
    "assets" list unconditionally -- for the (default) single-variant case
    this rendered the SAME generated image twice in the assistant bubble.

No real provider/network calls anywhere in this file. No Railway/deploy
operations, no production writes, no secrets printed.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

import numpy as np
from PIL import Image

from business_assistant.action_continuation import (
    FAMILY_IMAGE_GENERATE,
    format_tool_user_text,
)
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from integrations.production.adapters.media import OpenAIImageGenerationProvider
from integrations.production.errors import ProviderErrorCategory
from product_media.errors import MediaError
from product_media.providers.fake import FakeImageGenerationProvider
from product_media.runtime import build_product_media_runtime
from product_media.sqlite_store import SqliteMediaStore
from side_effects.runtime import compose_side_effect_runtime

NOT_A_REAL_KEY = "sk-test-not-a-real-openai-key-77777"


def _fake_image_bytes(size_bytes: int) -> bytes:
    """Deterministic, incompressible-enough payload of an exact byte length
    (PNG-signature-prefixed so it superficially resembles real image bytes;
    content doesn't need to decode -- only the HTTP-layer size gate is under
    test here)."""
    return b"\x89PNG\r\n\x1a\n" + os.urandom(max(0, size_bytes - 8))


def _real_large_png(min_bytes: int, edge: int = 900) -> bytes:
    """A genuinely valid, PIL-decodable PNG whose encoded size exceeds
    min_bytes -- random noise is essentially incompressible, giving a
    realistic stand-in for a detailed AI-generated photo (unlike a flat/
    simple graphic, which PNG would compress far below any real threshold)."""
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 256, (edge, edge, 3), dtype="uint8")
    raw = io.BytesIO()
    Image.fromarray(arr).save(raw, format="PNG")
    data = raw.getvalue()
    assert len(data) >= min_bytes, f"fixture too small: {len(data)} < {min_bytes}"
    return data


class ResponseSizeLimitTests(unittest.TestCase):
    """DEFECT 1: the HTTP layer must not discard a genuinely successful,
    realistically-sized b64_json image response."""

    def test_provider_configures_a_generous_response_size_limit(self):
        """Must be comfortably above the old, too-small 1 MiB generic default
        -- large enough for a real base64-encoded 1024x1024+ PNG response."""
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="gpt-image-1")
        self.assertGreaterEqual(provider._http.max_response_bytes, 8 * 1024 * 1024)

    def _response_for_raw_png_size(self, raw_png_bytes: int):
        raw = _fake_image_bytes(raw_png_bytes)
        b64 = base64.b64encode(raw).decode()
        body = json.dumps({"data": [{"b64_json": b64}]}).encode()
        return raw, body

    def test_realistic_1_5mb_image_no_longer_rejected_as_too_large(self):
        """A 1.5 MB raw PNG (realistic for a detailed 1024x1024 AI-generated
        image) base64-encodes to ~2 MB -- comfortably over the OLD 1 MiB
        cap, but must now succeed end-to-end."""
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="gpt-image-1")
        raw, body = self._response_for_raw_png_size(1_500_000)
        self.assertGreater(len(body), 1_048_576, "fixture must exceed the OLD 1 MiB cap to be meaningful")

        http_response = Mock()
        http_response.status_code = 200
        http_response.content = body
        http_response.headers = {}
        http_response.json = Mock(return_value=json.loads(body))
        inner_client = Mock()
        inner_client.request = Mock(return_value=http_response)
        provider._http._client = inner_client

        result = provider.generate(prompt="красная кружка на белом фоне")
        self.assertEqual(result.data, raw)

    def test_response_still_bounded_against_unbounded_memory_use(self):
        """Sanity: the cap is generous, not removed -- an absurdly oversized
        response must still be rejected rather than consuming unbounded
        memory."""
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="gpt-image-1")
        oversized = b"x" * (provider._http.max_response_bytes + 1)
        http_response = Mock()
        http_response.status_code = 200
        http_response.content = oversized
        http_response.headers = {}
        inner_client = Mock()
        inner_client.request = Mock(return_value=http_response)
        provider._http._client = inner_client
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")


class SqliteMediaStoreDirectoryCreationTests(unittest.TestCase):
    """DEFECT 2: a configured on-disk path whose parent directory does not
    yet exist (Railway volume mount gotcha) must not disable the entire
    product-media subsystem."""

    def test_creates_missing_nested_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "nested", "volume", "media.db")
            self.assertFalse(os.path.isdir(os.path.dirname(db_path)))
            store = SqliteMediaStore(db_path)
            self.assertTrue(os.path.isdir(os.path.dirname(db_path)))
            self.assertTrue(os.path.isfile(db_path))
            store._conn.close()

    def test_in_memory_path_unaffected(self):
        store = SqliteMediaStore(":memory:")
        store._conn.close()

    def test_full_runtime_survives_previously_missing_directory_and_generates(self):
        """End-to-end: build_product_media_runtime() with a not-yet-created
        nested directory must still produce a working, generating service --
        not the silent None fallback that disables image.generate entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "railway-volume", "data", "media.db")
            service = build_product_media_runtime(db_path=db_path)
            service.generator = FakeImageGenerationProvider()
            result = service.generate_from_brief(tenant_id="tenant-a", scene_description="панда")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["version_ids"]), 1)


class DuplicateImageTextTests(unittest.TestCase):
    """DEFECT 3: a single generated image must render exactly once in the
    assistant's text bubble (independent of the frontend-level dedup guard
    added in the prior CLOSED block, which only protects against app.js
    re-adding an image already embedded in the bubble -- it cannot fix a
    duplicate embedded WITHIN the bubble text itself)."""

    def _payload(self, n: int):
        assets = [
            {
                "version_id": f"v{i}",
                "artifact_type": "image",
                "mime_type": "image/png",
                "view_url": f"/api/v1/business-assistant/media/v{i}",
            }
            for i in range(n)
        ]
        return {
            "version_ids": [a["version_id"] for a in assets],
            "assets": assets,
            "mime_type": "image/png",
            "view_url": assets[0]["view_url"] if assets else "",
            "status": "completed",
            "failed": 0,
        }

    def test_single_variant_renders_exactly_one_image(self):
        text = format_tool_user_text(family=FAMILY_IMAGE_GENERATE, data=self._payload(1), success=True)
        self.assertEqual(text.count("!["), 1)

    def test_two_variants_render_exactly_two_distinct_images(self):
        text = format_tool_user_text(family=FAMILY_IMAGE_GENERATE, data=self._payload(2), success=True)
        self.assertEqual(text.count("!["), 2)
        self.assertIn("/v0", text)
        self.assertIn("/v1", text)

    def test_version_ids_only_payload_without_assets_still_renders_one_image(self):
        payload = {"version_ids": ["v9"], "view_url": "/api/v1/business-assistant/media/v9", "status": "completed"}
        text = format_tool_user_text(family=FAMILY_IMAGE_GENERATE, data=payload, success=True)
        self.assertEqual(text.count("!["), 1)
        self.assertIn("/v9", text)

    def test_end_to_end_conversation_reply_has_no_duplicate_image(self):
        runtime = compose_side_effect_runtime(env={})
        runtime.product_media_service.generator = FakeImageGenerationProvider()
        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "unused"})
        engine.last_workflow_id = "wf-1"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
        )
        import asyncio

        result = asyncio.run(
            gw.respond(
                ConversationRequest(
                    text="Создай изображение красной кружки на белом фоне",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    request_id="req-dup-text-1",
                    conversation_id="c1",
                )
            )
        )
        self.assertEqual(result.text.count("!["), 1, result.text)
        self.assertEqual(len(result.metadata.get("artifacts") or []), 1)


class ExactReportedScenarioEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """The exact reported production prompt, now succeeding end-to-end with
    a realistically-sized mocked gpt-image-1 response (large enough to have
    tripped the old 1 MiB cap) and a single, non-duplicated rendered image."""

    async def test_red_mug_prompt_succeeds_with_realistic_large_response(self):
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="gpt-image-1")
        raw = _real_large_png(1_200_000)
        body = json.dumps({"data": [{"b64_json": base64.b64encode(raw).decode()}]}).encode()
        self.assertGreater(len(body), 1_048_576)
        http_response = Mock()
        http_response.status_code = 200
        http_response.content = body
        http_response.headers = {}
        http_response.json = Mock(return_value=json.loads(body))
        inner_client = Mock()
        inner_client.request = Mock(return_value=http_response)
        provider._http._client = inner_client

        runtime = compose_side_effect_runtime(env={})
        runtime.product_media_service.generator = provider
        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "unused"})
        engine.last_workflow_id = "wf-1"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
        )
        result = await gw.respond(
            ConversationRequest(
                text="Создай изображение красной кружки на белом фоне",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-red-mug-1",
                conversation_id="c1",
            )
        )
        self.assertIn("Готово.", result.text)
        self.assertEqual(result.text.count("!["), 1, result.text)
        artifacts = result.metadata.get("artifacts") or []
        self.assertEqual(len(artifacts), 1)
        self.assertTrue(artifacts[0]["view_url"])


if __name__ == "__main__":
    unittest.main()
