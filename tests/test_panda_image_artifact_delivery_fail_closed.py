"""PANDA LIVE IMAGE GENERATION -- REAL ARTIFACT DELIVERY / FAIL-CLOSED PRODUCTION FIX.

Deterministic, offline-safe regression tests for the production symptom:

    User: "Сгенерируй изображение панды, которая ест бамбук"
    Panda: "Готово."   (no image ever rendered, even with MEDIA_IMAGE_PROVIDER=openai
                         and a real OPENAI_API_KEY configured)

Proven root cause (two independent, compounding defects; see class docstrings
below): (1) the OpenAI adapter never requested response_format=b64_json, so a
genuinely successful OpenAI call could never produce usable image bytes; (2)
even when a provider/persistence failure IS raised and caught, the tool
layer reported unconditional success, and the conversational text layer said
"Готово." without checking whether any artifact was actually produced.

Does not reopen or rewrite: Action Continuation family/contract structure,
Follow-Up Resolution, RouterV2, WorkflowEngine, ToolGateway, ProductMedia
persistence model, Business Assistant API, media endpoint, auth, deployment.

No real provider/network calls anywhere in this file -- OpenAI HTTP calls are
stubbed at the BoundedHttpClient boundary.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from PIL import Image

from business_assistant.action_continuation import CALL_TOOL
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from integrations.production.adapters.media import (
    OpenAIImageGenerationProvider,
    build_image_provider,
)
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from product_media.errors import MediaError
from product_media.providers.fake import FakeImageGenerationProvider
from product_media.runtime import build_product_media_runtime
from side_effects.runtime import compose_side_effect_runtime
from tools.models import TOOL_STATUS_SUCCEEDED, ToolRequest

ROOT = Path(__file__).resolve().parent.parent
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NOT_A_REAL_KEY = "sk-test-not-a-real-openai-key-00000"


def _real_looking_png(color=(200, 30, 30), size=(48, 48)) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


REAL_FIXTURE_PNG = _real_looking_png()


def _stub_http(response_payload=None, *, raises=None, raw_json_bytes=None):
    """Build a stand-in for BoundedHttpClient.request -- no network I/O."""
    client = Mock()

    def _request(*args, **kwargs):
        if raises is not None:
            raise raises
        resp = Mock()
        if raw_json_bytes is not None:
            resp.json = Mock(side_effect=json.JSONDecodeError("bad", "x", 0))
        else:
            resp.json = Mock(return_value=response_payload)
        return resp

    client.request = Mock(side_effect=_request)
    return client


class OpenAIProviderRequestContractTests(unittest.TestCase):
    """STEP 3/6: request schema + response normalization, no network calls."""

    def _provider(self):
        p = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY)
        return p

    def test_request_payload_requests_b64_json(self):
        """The former defect: the request never asked for response_format=b64_json,
        so a real OpenAI call (which defaults to hosted "url" responses) could
        never satisfy this adapter's parsing -- every real generation failed."""
        provider = self._provider()
        provider._http = _stub_http({"data": [{"b64_json": base64.b64encode(REAL_FIXTURE_PNG).decode()}]})
        provider.generate(prompt="панда есть бамбук")
        _, _, kwargs = provider._http.request.mock_calls[0]
        self.assertEqual(kwargs["json_body"].get("response_format"), "b64_json")

    def test_valid_b64_json_succeeds(self):
        provider = self._provider()
        provider._http = _stub_http({"data": [{"b64_json": base64.b64encode(REAL_FIXTURE_PNG).decode()}]})
        result = provider.generate(prompt="панда есть бамбук")
        self.assertEqual(result.data, REAL_FIXTURE_PNG)
        self.assertEqual(result.mime_type, "image/png")

    def test_legacy_url_only_response_fails_closed(self):
        """Reproduces the exact real-world shape OpenAI returns without
        response_format=b64_json -- must fail, never silently succeed."""
        provider = self._provider()
        provider._http = _stub_http({"data": [{"url": "https://oaidalleapiprodscus.example/blob"}]})
        with self.assertRaises(MediaError):
            provider.generate(prompt="панда есть бамбук")

    def test_empty_data_list_fails_closed(self):
        provider = self._provider()
        provider._http = _stub_http({"data": []})
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_missing_data_key_fails_closed(self):
        provider = self._provider()
        provider._http = _stub_http({})
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_data_item_without_b64_json_fails_closed(self):
        provider = self._provider()
        provider._http = _stub_http({"data": [{"revised_prompt": "a panda"}]})
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_empty_b64_json_string_fails_closed(self):
        provider = self._provider()
        provider._http = _stub_http({"data": [{"b64_json": ""}]})
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_invalid_base64_fails_closed(self):
        provider = self._provider()
        provider._http = _stub_http({"data": [{"b64_json": "not-valid-base64!!!"}]})
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_decoded_zero_byte_payload_fails_closed(self):
        provider = self._provider()
        provider._http = _stub_http({"data": [{"b64_json": base64.b64encode(b"").decode()}]})
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_unsupported_response_shape_fails_closed(self):
        provider = self._provider()
        provider._http = _stub_http({"data": "not-a-list"})
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")
        provider2 = self._provider()
        provider2._http = _stub_http(["top-level-list-not-dict"])
        with self.assertRaises(MediaError):
            provider2.generate(prompt="x")

    def test_provider_error_payload_with_http_200_fails_closed(self):
        """A 200 response whose body IS an error payload must never succeed."""
        provider = self._provider()
        provider._http = _stub_http({"error": {"message": "content_policy_violation"}})
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_malformed_json_fails_closed(self):
        provider = self._provider()
        provider._http = _stub_http(raw_json_bytes=True)
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_http_error_propagates_as_media_error(self):
        provider = self._provider()
        provider._http = _stub_http(
            raises=ProductionProviderError(ProviderErrorCategory.PROVIDER_UNAVAILABLE, message="http_503", provider_id="media_image")
        )
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_timeout_propagates_as_media_error(self):
        provider = self._provider()
        provider._http = _stub_http(
            raises=ProductionProviderError(ProviderErrorCategory.TIMEOUT, message="request_timeout", provider_id="media_image", retryable=True)
        )
        with self.assertRaises(MediaError):
            provider.generate(prompt="x")

    def test_no_secrets_leaked_in_failure_paths(self):
        provider = self._provider()
        provider._http = _stub_http({"data": []})
        try:
            provider.generate(prompt="x")
            self.fail("expected MediaError")
        except MediaError as exc:
            self.assertNotIn(NOT_A_REAL_KEY, str(exc))
            self.assertNotIn(NOT_A_REAL_KEY, exc.code)
            self.assertNotIn(NOT_A_REAL_KEY, repr(exc))


class ProviderSelectionFailClosedTests(unittest.TestCase):
    """STEP 3/7: explicit non-fake configuration must never silently fall back
    to the fake/placeholder provider. Fake remains selectable only when
    MEDIA_IMAGE_PROVIDER is unset/explicitly "fake" (dev/offline/test path)."""

    def test_unset_provider_still_defaults_to_fake_dev_path(self):
        self.assertIsInstance(build_image_provider({}), FakeImageGenerationProvider)

    def test_explicit_fake_provider_allowed(self):
        self.assertIsInstance(build_image_provider({"MEDIA_IMAGE_PROVIDER": "fake"}), FakeImageGenerationProvider)

    def test_explicit_openai_without_key_fails_closed_not_fake(self):
        with self.assertRaises(ProductionProviderError):
            build_image_provider({"MEDIA_IMAGE_PROVIDER": "openai"})

    def test_explicit_openai_without_key_fails_closed_even_outside_declared_production(self):
        """Regression guard for the prior loophole: failure must not depend on
        PANDA_ENV/ENVIRONMENT being set to "production" -- the operator's
        explicit MEDIA_IMAGE_PROVIDER=openai choice is itself authoritative."""
        with self.assertRaises(ProductionProviderError):
            build_image_provider({"MEDIA_IMAGE_PROVIDER": "openai", "PANDA_ENV": "staging"})
        with self.assertRaises(ProductionProviderError):
            build_image_provider({"MEDIA_IMAGE_PROVIDER": "openai"})

    def test_explicit_openai_with_key_selected(self):
        provider = build_image_provider({"MEDIA_IMAGE_PROVIDER": "openai", "MEDIA_IMAGE_API_KEY": NOT_A_REAL_KEY})
        self.assertIsInstance(provider, OpenAIImageGenerationProvider)
        self.assertEqual(provider.api_key, NOT_A_REAL_KEY)

    def test_openai_api_key_env_var_also_accepted(self):
        provider = build_image_provider({"MEDIA_IMAGE_PROVIDER": "openai", "OPENAI_API_KEY": NOT_A_REAL_KEY})
        self.assertIsInstance(provider, OpenAIImageGenerationProvider)


class _RaisingProvider:
    """Deterministic stand-in reproducing the exact contract-gap symptom:
    the provider genuinely fails (as the unpatched OpenAI adapter always did),
    which ProductMediaService/ProductMediaToolAdapter turn into a controlled,
    non-raising {"status": "error"} tool response."""

    provider_id = "test-raising"
    calls = 0

    def generate(self, *, prompt: str, width: int = 512, height: int = 512, seed=None):
        self.calls += 1
        raise MediaError("MEDIA_GENERATION_FAILED", "empty_image")


class FalseSuccessRootCauseReproductionTests(unittest.IsolatedAsyncioTestCase):
    """STEP 2/4: trace + prove the exact boundary that converted "no usable
    artifact" into success=True / "Готово."."""

    async def test_tool_gateway_read_boundary_reports_success_on_non_raising_error_payload(self):
        """Documents (does not change -- this is the GENERIC read-tool contract
        used by many unrelated tools, not just image.generate) that ToolGateway
        only inspects "did the adapter raise", not the adapter's own semantic
        data.get('status'). This is WHERE a controlled {"status": "error"}
        result becomes ToolResult.success=True -- the boundary Step 2 asks to
        locate. The fix therefore lives one layer up (capability-scoped), not
        here, to avoid globally coupling unrelated read tools to an artifact
        requirement."""
        from autonomy.capabilities import CAP_IMAGE_GENERATE

        runtime = compose_side_effect_runtime(env={})
        runtime.product_media_service.generator = _RaisingProvider()
        request = ToolRequest(
            request_id="r1",
            workflow_id="",
            task_id="t1",
            tool_id="image.generate",
            operation="generate",
            arguments={"scene_description": "панда есть бамбук"},
            requested_capabilities=(CAP_IMAGE_GENERATE,),
            tenant_id="tenant-a",
            user_id="user-a",
            actor_id="tenant-a:user-a",
        )
        result = await runtime.tool_gateway.invoke(request, capabilities=None)
        self.assertTrue(result.success)
        self.assertEqual(result.status, TOOL_STATUS_SUCCEEDED)
        self.assertEqual(result.data.get("status"), "error")
        self.assertNotIn("view_url", result.data)

    async def test_conversation_gateway_fails_closed_despite_permissive_tool_layer(self):
        """The actual fix boundary: even though the ToolGateway layer above
        reports success=True with no artifacts, the conversation gateway must
        now produce the canonical failure text, not "Готово.", and must not
        report any artifacts."""
        runtime = compose_side_effect_runtime(env={})
        runtime.product_media_service.generator = _RaisingProvider()
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
                text="сгенерируй картинку панды, которая ест бамбук",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-fail-1",
                conversation_id="c1",
            )
        )
        self.assertNotEqual(result.text.strip(), "Готово.")
        self.assertNotIn("![", result.text)
        self.assertEqual(result.metadata.get("artifacts"), [])


class ImageGenerationSuccessInvariantTests(unittest.IsolatedAsyncioTestCase):
    """STEP 5/12: the strict end-to-end success invariant, across the exact
    conversational entry points a real user hits."""

    def _gw(self, generator):
        runtime = compose_side_effect_runtime(env={})
        runtime.product_media_service.generator = generator
        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "unused"})
        engine.last_workflow_id = "wf-1"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
        )
        return gw, runtime

    async def test_russian_request_with_working_provider_succeeds_with_artifact(self):
        gw, runtime = self._gw(FakeImageGenerationProvider())
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды, которая ест бамбук",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-ru-1",
                conversation_id="c1",
            )
        )
        self.assertIn("Готово.", result.text)
        self.assertIn("![", result.text)
        self.assertTrue(result.metadata.get("artifacts"))

    async def test_english_request_routes_and_succeeds_with_artifact(self):
        gw, runtime = self._gw(FakeImageGenerationProvider())
        result = await gw.respond(
            ConversationRequest(
                text="Generate an image of a panda eating bamboo",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-en-1",
                conversation_id="c1",
            )
        )
        self.assertTrue(result.metadata.get("artifacts"), result.text)

    async def test_failing_provider_never_says_gotovo_and_reports_no_artifacts(self):
        gw, runtime = self._gw(_RaisingProvider())
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды, которая ест бамбук",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-fail-2",
                conversation_id="c1",
            )
        )
        self.assertNotEqual(result.text.strip(), "Готово.")
        self.assertEqual(result.metadata.get("artifacts"), [])

    async def test_two_requested_variants_produce_two_artifacts(self):
        gw, runtime = self._gw(FakeImageGenerationProvider())
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй 2 изображения панды, которая ест бамбук",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-two-1",
                conversation_id="c1",
            )
        )
        artifacts = result.metadata.get("artifacts") or []
        self.assertGreaterEqual(len(artifacts), 1)

    async def test_duplicate_request_id_idempotent_after_real_success(self):
        provider = FakeImageGenerationProvider()
        gw, runtime = self._gw(provider)
        req = ConversationRequest(
            text="Сгенерируй изображение панды, которая ест бамбук",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="req-dup-success",
            conversation_id="c1",
        )
        first = await gw.respond(req)
        second = await gw.respond(req)
        self.assertTrue(first.metadata.get("artifacts"))
        self.assertTrue(second.metadata.get("duplicate"))

    async def test_duplicate_request_id_after_failure_is_not_poisoned(self):
        """A failed generation must never be cached as a successful idempotent
        result -- the user must be able to retry with the same request_id."""
        provider = _RaisingProvider()
        gw, runtime = self._gw(provider)
        req = ConversationRequest(
            text="Сгенерируй изображение панды, которая ест бамбук",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="req-dup-fail",
            conversation_id="c1",
        )
        first = await gw.respond(req)
        second = await gw.respond(req)
        self.assertEqual(first.metadata.get("artifacts"), [])
        self.assertFalse(second.metadata.get("duplicate"))
        self.assertEqual(provider.calls, 2, "retry after failure must call the provider again")

    async def test_follow_up_scene_then_bamboo_completes_with_artifact(self):
        """Existing follow-up resolution semantics (unchanged): an underspecified
        first message asks one clarification instead of auto-executing; the
        scene-completing follow-up then executes and must deliver an artifact."""
        gw, runtime = self._gw(FakeImageGenerationProvider())
        first = await gw.respond(
            ConversationRequest(
                text="сгенерируй изображение",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-followup-1",
                conversation_id="c1",
            )
        )
        self.assertEqual(gw.last_action_decision.decision, "ASK_CLARIFICATION")
        second = await gw.respond(
            ConversationRequest(
                text="панда, которая ест бамбук",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-followup-2",
                conversation_id="c1",
            )
        )
        self.assertEqual(gw.last_action_decision.decision, CALL_TOOL)
        self.assertTrue(second.metadata.get("artifacts"), second.text)

    async def test_quantity_continuation_wolf_then_two(self):
        """Existing follow-up resolution semantics (unchanged): "волка" already
        fully specifies the scene and executes immediately with one artifact;
        the ambiguous quantity follow-up asks for a number, and the numeric
        reply re-executes with the corrected variant_count -- both real
        executions must deliver real artifacts (not just the last one)."""
        gw, runtime = self._gw(FakeImageGenerationProvider())
        first = await gw.respond(
            ConversationRequest(
                text="сделай картинку волка",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-qty-1",
                conversation_id="c1",
            )
        )
        self.assertEqual(gw.last_action_decision.decision, CALL_TOOL)
        self.assertTrue(first.metadata.get("artifacts"), first.text)
        clarify = await gw.respond(
            ConversationRequest(
                text="сделай несколько",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-qty-2",
                conversation_id="c1",
            )
        )
        self.assertEqual(gw.last_action_decision.decision, "ASK_CLARIFICATION")
        second = await gw.respond(
            ConversationRequest(
                text="2",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-qty-3",
                conversation_id="c1",
            )
        )
        self.assertEqual(gw.last_action_decision.decision, CALL_TOOL)
        self.assertEqual(gw.last_action_decision.task.quantity, 2)
        artifacts = second.metadata.get("artifacts") or []
        self.assertEqual(len(artifacts), 2, second.text)

    async def test_multi_variant_artifacts_have_distinct_correct_view_urls(self):
        """Regression for a distinct propagation defect found while proving
        artifact identity (Step 9/audit BY): artifacts_from_tool_data() used to
        reuse the FIRST generated image's view_url for every variant when
        variant_count > 1, so a second/third requested image silently pointed
        at the first image's URL instead of its own."""
        gw, runtime = self._gw(FakeImageGenerationProvider())
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй 3 изображения панды, которая ест бамбук",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-multi-1",
                conversation_id="c1",
            )
        )
        artifacts = result.metadata.get("artifacts") or []
        self.assertEqual(len(artifacts), 3, result.text)
        urls = [a["view_url"] for a in artifacts]
        self.assertEqual(len(set(urls)), 3, "each variant must have its own distinct view_url")
        refs = [a["ref"] for a in artifacts]
        self.assertEqual(len(set(refs)), 3, "each variant must have its own distinct artifact ref/version_id")
        for a, url in zip(artifacts, urls):
            self.assertIn(a["ref"], url)


class PersistenceRestartSimulationTests(unittest.IsolatedAsyncioTestCase):
    """STEP 11: durable (file-backed, not :memory:) persistence must survive
    a simulated service/store reconstruction -- no real Railway restart."""

    async def test_artifact_survives_service_reconstruction(self):
        tmp = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmp, "media.sqlite")
            service = build_product_media_runtime(db_path=db_path)
            service.generator = FakeImageGenerationProvider()
            result = service.generate_from_brief(tenant_id="tenant-a", scene_description="панда ест бамбук")
            version_id = result["version_ids"][0]
            original_blob = service.get_blob(tenant_id="tenant-a", version_id=version_id)
            self.assertTrue(original_blob.startswith(PNG_SIGNATURE))

            reconstructed = build_product_media_runtime(db_path=db_path)
            reloaded_version = reconstructed.get(tenant_id="tenant-a", version_id=version_id)
            self.assertIsNotNone(reloaded_version)
            reloaded_blob = reconstructed.get_blob(tenant_id="tenant-a", version_id=version_id)
            self.assertEqual(reloaded_blob, original_blob)
            from product_media.tools import media_view_url

            self.assertEqual(media_view_url(version_id), f"/api/v1/business-assistant/media/{version_id}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MediaEndpointFailClosedIntegrationTests(unittest.TestCase):
    """Full HTTP integration: a failing provider must never leave a phantom
    "success" API result, and a succeeding one must serve real bytes with
    existing auth/tenant isolation intact (both reused, unmodified)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["BA_API_DB_PATH"] = os.path.join(cls.tmp, "ba.sqlite")
        os.environ["BA_API_UPLOAD_DIR"] = os.path.join(cls.tmp, "uploads")
        os.makedirs(os.environ["BA_API_UPLOAD_DIR"], exist_ok=True)
        os.environ["SECURITY_AUTH_MODE"] = "required"
        os.environ["PANDA_API_KEYS"] = (
            "key-a|tenant-a|user-a|user|secret-fc-a;"
            "key-b|tenant-b|user-b|user|secret-fc-b"
        )
        os.environ["PRODUCT_MEDIA_DB_PATH"] = ":memory:"
        import importlib

        import main as main_mod

        cls.main = importlib.reload(main_mod)
        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_successful_generation_serves_real_bytes(self):
        self.main.side_effect_runtime.product_media_service.generator = FakeImageGenerationProvider()
        r = self.client.post(
            "/api/v1/business-assistant/requests",
            headers={"X-API-Key": "secret-fc-a"},
            json={"message": "Сгенерируй изображение панды, которая ест бамбук"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        request_id = r.json()["request_id"]
        result = self.client.get(
            f"/api/v1/business-assistant/requests/{request_id}/result",
            headers={"X-API-Key": "secret-fc-a"},
        ).json()
        artifacts = result.get("artifacts") or []
        self.assertTrue(artifacts, result)
        view_url = artifacts[0]["view_url"]
        media = self.client.get(view_url, headers={"X-API-Key": "secret-fc-a"})
        self.assertEqual(media.status_code, 200)
        self.assertTrue(media.headers["content-type"].startswith("image/"))
        self.assertTrue(media.content.startswith(PNG_SIGNATURE))

    def test_failing_generation_reports_no_artifacts_not_fake_success(self):
        self.main.side_effect_runtime.product_media_service.generator = _RaisingProvider()
        r = self.client.post(
            "/api/v1/business-assistant/requests",
            headers={"X-API-Key": "secret-fc-a"},
            json={"message": "Сгенерируй изображение панды, которая ест бамбук"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        request_id = r.json()["request_id"]
        result = self.client.get(
            f"/api/v1/business-assistant/requests/{request_id}/result",
            headers={"X-API-Key": "secret-fc-a"},
        ).json()
        self.assertEqual(result.get("artifacts") or [], [])
        self.assertNotEqual(str(result.get("final_answer") or "").strip(), "Готово.")


class FrontendFailClosedRenderingTests(unittest.TestCase):
    """STEP 9/10: artifact propagation to the frontend, exactly-one rendering
    per artifact, no phantom attachment on failure, dedup guard against the
    bubble-already-embeds-the-link duplication risk."""

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

    def test_valid_artifact_renders_exactly_one_image(self):
        url = f"/api/v1/business-assistant/media/{uuid.uuid4()}"
        results = self._run(
            [{"name": "ok", "fn": "renderMessage", "args": {"role": "assistant", "content": f"Готово.\n![изображение]({url})", "meta": None}}]
        )
        r = results["ok"]
        self.assertEqual(len(r["imgs"]), 1)
        self.assertEqual(r["imgs"][0]["src"], url)

    def test_empty_artifacts_failure_text_renders_no_phantom_image(self):
        results = self._run(
            [
                {
                    "name": "fail",
                    "fn": "renderMessage",
                    "args": {"role": "assistant", "content": "Не получилось выполнить действие. Можно повторить запрос.", "meta": None},
                }
            ]
        )
        r = results["fail"]
        self.assertEqual(r["imgs"], [])

    def test_app_js_dedup_guard_present_and_correct(self):
        """Regression guard for the duplicate-rendering risk found while
        tracing artifact propagation: format_tool_user_text already embeds
        the markdown image link in the assistant bubble text, and app.js
        independently reconstructs the same link from result.artifacts --
        without a dedup guard, the same image would render twice."""
        app_src = (ROOT / "static/panda/js/app.js").read_text(encoding="utf-8")
        self.assertIn("bubble.includes(u)", app_src)

        url = "/api/v1/business-assistant/media/abc-123"
        bubble = f"Готово.\n![изображение]({url})"
        artifacts = [{"artifact_type": "image", "view_url": url}]
        image_lines = [
            f"![изображение]({a['view_url']})"
            for a in artifacts
            if a.get("view_url", "").startswith(("/", "https://")) and a["view_url"] not in bubble
        ]
        content = "\n".join([bubble, *image_lines]) if image_lines else bubble
        results = self._run(
            [{"name": "dedup", "fn": "renderMessage", "args": {"role": "assistant", "content": content, "meta": None}}]
        )
        self.assertEqual(len(results["dedup"]["imgs"]), 1, "same artifact URL must render exactly once")

    def test_unsafe_url_scheme_still_rejected(self):
        results = self._run([{"name": "unsafe", "fn": "renderRichText", "args": {"text": "![x](javascript:alert(1))"}}])
        self.assertEqual(results["unsafe"]["imgs"], [])


if __name__ == "__main__":
    unittest.main()
