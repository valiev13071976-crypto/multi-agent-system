"""PANDA IMAGE GENERATION — FINAL FIX.

Root cause (proven from code, reproduced below):

`side_effects/runtime.py::_finalize_runtime()` builds the product-media
subsystem (the real OpenAI image provider + persistent store) inside a bare
``try/except Exception: product_media_runtime = None``. ANY exception raised
while constructing it -- including the intentional, fail-closed
``ProductionProviderError(CONFIGURATION_ERROR, message="image_key_required")``
that ``build_image_provider()`` raises when ``MEDIA_IMAGE_PROVIDER`` is
explicitly set to a real provider (e.g. "openai") but no API key is
configured, or any other real startup defect (bad model string, unwritable
storage path, etc.) -- was swallowed completely silently: no log line, no
exception, nothing. ``engine.product_media_service`` simply stayed ``None``.

Compounding this, the existing zero-network ``check_image_generation_
readiness()`` diagnostic (added in a previous fix specifically to make this
class of problem diagnosable) was only ever invoked from ``.health()`` when
``self.product_media_service is not None`` -- i.e. it could never run in
exactly the one scenario it exists to diagnose.

The net effect: whenever product-media construction failed for any reason,
`image.generate` became permanently unavailable
(``ProductMediaToolAdapter.execute_read`` raises ``ToolNotFoundError`` because
``self.service is None``), every real user request failed closed with the
generic "Не получилось выполнить действие. Можно повторить запрос." -- and
there was categorically NO way to see why from application/Railway logs,
exactly matching the reported production symptom (HTTP 200 throughout, no
visible provider/media exception anywhere).

Fix: log a safe, structured `product_media_construction_failed` diagnostic
(no secrets -- ProductionProviderError.__str__ never embeds a credential
value) when construction fails, retain the failure reason on the runtime,
and make `.health()`'s `image_generation_readiness` always evaluate --
including a new `PRODUCT_MEDIA_SERVICE=CONSTRUCTED` /
`CONSTRUCTION_FAILED(<reason>)` line -- so the true cause is now always
diagnosable from a running deployment without any code change.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import tempfile
import unittest

import numpy as np
from PIL import Image

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_ENV_KEYS = ("MEDIA_IMAGE_PROVIDER", "MEDIA_IMAGE_API_KEY", "OPENAI_API_KEY", "MEDIA_IMAGE_MODEL")


def _real_large_png(min_bytes: int, edge: int = 900) -> bytes:
    """A genuinely valid, PIL-decodable PNG whose encoded size exceeds
    min_bytes -- a realistic stand-in for a detailed AI-generated image
    (random noise is essentially incompressible, unlike a flat/simple
    graphic)."""
    rng = np.random.default_rng(11)
    arr = rng.integers(0, 256, (edge, edge, 3), dtype="uint8")
    raw = io.BytesIO()
    Image.fromarray(arr).save(raw, format="PNG")
    data = raw.getvalue()
    assert len(data) >= min_bytes, f"fixture too small: {len(data)} < {min_bytes}"
    return data


class ProductMediaConstructionFailureDiagnosticsTests(unittest.TestCase):
    """ROOT CAUSE regression: a product-media construction failure must be
    logged and surfaced via health readiness, never silently swallowed."""

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_key_with_explicit_real_provider_fails_closed_with_logged_diagnostic(self):
        os.environ["MEDIA_IMAGE_PROVIDER"] = "openai"
        # MEDIA_IMAGE_API_KEY / OPENAI_API_KEY intentionally left unset.
        from side_effects.runtime import compose_side_effect_runtime

        with self.assertLogs("side_effects.runtime", level="ERROR") as captured:
            runtime = compose_side_effect_runtime()

        self.assertIsNone(runtime.product_media_service, "must fail CLOSED, never fall back to fake silently")
        self.assertTrue(runtime.product_media_construction_error, "failure reason must be retained")
        self.assertIn("image_key_required", runtime.product_media_construction_error)

        joined_logs = "\n".join(captured.output)
        self.assertIn("product_media_construction_failed", joined_logs)
        self.assertIn("ProductionProviderError", joined_logs)
        self.assertIn("image_key_required", joined_logs)
        # No secret value (there isn't one configured here, but assert the
        # structural guarantee: only the safe, static error taxonomy string
        # -- category:provider_id:message -- ever appears, never a raw key).
        self.assertNotIn("sk-", joined_logs)

    def test_health_readiness_surfaces_construction_failure_reason_not_ready(self):
        os.environ["MEDIA_IMAGE_PROVIDER"] = "openai"
        from side_effects.runtime import compose_side_effect_runtime

        logging.getLogger("side_effects.runtime").setLevel(logging.CRITICAL)
        try:
            runtime = compose_side_effect_runtime()
        finally:
            logging.getLogger("side_effects.runtime").setLevel(logging.NOTSET)

        health = runtime.health()
        readiness = health.metadata.get("image_generation_readiness")
        self.assertIsNotNone(readiness, "readiness must be evaluated even when construction failed")
        self.assertEqual(readiness["status"], "NOT_READY")
        reasons = readiness["reasons"]
        self.assertTrue(
            any(r.startswith("PRODUCT_MEDIA_SERVICE=CONSTRUCTION_FAILED(") for r in reasons),
            reasons,
        )

    def test_successful_construction_reports_constructed_and_ready_reason(self):
        # No override: default provider is "fake" -- constructs successfully.
        from side_effects.runtime import compose_side_effect_runtime

        runtime = compose_side_effect_runtime()
        self.assertIsNotNone(runtime.product_media_service)
        self.assertEqual(runtime.product_media_construction_error, "")
        health = runtime.health()
        readiness = health.metadata.get("image_generation_readiness")
        self.assertIsNotNone(readiness)
        self.assertIn("PRODUCT_MEDIA_SERVICE=CONSTRUCTED", readiness["reasons"])


class OrangeWithEarsExactPromptFullHttpEndToEndTests(unittest.TestCase):
    """КРИТЕРИЙ ГОТОВНОСТИ: the exact reported production prompt succeeds
    end-to-end through the REAL HTTP API (not just the conversation gateway
    in isolation) with a realistic mocked gpt-image-1 response: русский
    запрос → image.generate → provider → b64_json decode → persistence →
    artifact/view_url → Business Assistant SUCCESS → API result → image
    bytes retrievable for Panda UI rendering."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["BA_API_DB_PATH"] = os.path.join(cls.tmp, "ba.sqlite")
        os.environ["BA_API_UPLOAD_DIR"] = os.path.join(cls.tmp, "uploads")
        os.makedirs(os.environ["BA_API_UPLOAD_DIR"], exist_ok=True)
        os.environ["SECURITY_AUTH_MODE"] = "required"
        os.environ["PANDA_API_KEYS"] = "key-orange|tenant-orange|user-orange|user|secret-orange"
        os.environ["PRODUCT_MEDIA_DB_PATH"] = ":memory:"
        os.environ.pop("MEDIA_IMAGE_PROVIDER", None)

        import importlib

        import main as main_mod

        cls.main = importlib.reload(main_mod)

        from fastapi.testclient import TestClient

        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_orange_with_ears_prompt_succeeds_end_to_end_with_realistic_gpt_image_response(self):
        from unittest.mock import Mock

        from integrations.production.adapters.media import OpenAIImageGenerationProvider

        provider = OpenAIImageGenerationProvider(api_key="sk-test-not-a-real-openai-key", model="gpt-image-1")
        raw = _real_large_png(1_400_000)
        body = json.dumps({"data": [{"b64_json": base64.b64encode(raw).decode()}]}).encode()
        self.assertGreater(len(body), 1_048_576, "fixture must exceed the historical 1 MiB HTTP cap to be meaningful")

        http_response = Mock()
        http_response.status_code = 200
        http_response.content = body
        http_response.headers = {}
        http_response.json = Mock(return_value=json.loads(body))
        inner_client = Mock()
        inner_client.request = Mock(return_value=http_response)
        provider._http._client = inner_client

        self.main.side_effect_runtime.product_media_service.generator = provider

        submit = self.client.post(
            "/api/v1/business-assistant/requests",
            headers={"X-API-Key": "secret-orange"},
            json={"message": "дай картинку апельсина с ушами"},
        )
        self.assertEqual(submit.status_code, 200, submit.text)
        request_id = submit.json()["request_id"]

        result = self.client.get(
            f"/api/v1/business-assistant/requests/{request_id}/result",
            headers={"X-API-Key": "secret-orange"},
        ).json()

        self.assertNotIn("Не получилось", str(result.get("final_answer") or ""), result)
        self.assertIn("Готово", str(result.get("final_answer") or ""), result)
        artifacts = result.get("artifacts") or []
        self.assertEqual(len(artifacts), 1, result)
        self.assertEqual(artifacts[0]["mime_type"], "image/png")
        view_url = artifacts[0]["view_url"]
        self.assertTrue(view_url)

        media = self.client.get(view_url, headers={"X-API-Key": "secret-orange"})
        self.assertEqual(media.status_code, 200)
        self.assertTrue(media.headers["content-type"].startswith("image/"))
        self.assertTrue(media.content.startswith(PNG_SIGNATURE))
        self.assertEqual(media.content, raw, "frontend must receive the EXACT decoded provider bytes")

        # Exactly one image reference in the assistant's rendered chat text --
        # no duplicate rendering of the same artifact.
        self.assertEqual(str(result.get("final_answer") or "").count("!["), 1, result)


if __name__ == "__main__":
    unittest.main()
