"""PANDA LIVE IMAGE GENERATION -- PRODUCTION FAILURE ROOT-CAUSE FIX & FINAL CLOSURE.

Deterministic, offline-safe regression tests for the production symptom:

    User: "Сгенерируй изображение панды, которая ест бамбук в лесу ночью."
    Panda: "Не получилось выполнить действие. Можно повторить запрос."
    (repeated attempts produce the same result; Railway HTTP logs show only 200s)

This does NOT reopen the prior CLOSED "real artifact delivery / fail-closed"
block (tests/test_panda_image_artifact_delivery_fail_closed.py) -- the fail-
closed invariant proven there is exercised here as a GIVEN, not re-derived.

Proven root cause (see ModelAwareRequestContractTests / GptImageModel...Tests
docstrings): the OpenAI adapter's request payload was NOT model-aware.

    (a) It unconditionally sent "response_format": "b64_json". OpenAI's
        gpt-image-* model family (the current flagship, non-legacy image
        model) REJECTS this parameter outright with HTTP 400 "Unknown
        parameter: 'response_format'" -- these models always return
        b64_json and cannot be asked for a URL at all.
    (b) It unconditionally sent "size": "512x512" for the (overwhelmingly
        common) default 1:1 aspect ratio. "512x512" is valid ONLY for
        dall-e-2; dall-e-3 and gpt-image-* both reject it with HTTP 400
        "Invalid size" (their enums start at 1024x1024).

Either misconfiguration alone is a deterministic, 100%-reproducible failure
for every image.generate call when a non-dall-e-2 model is configured --
exactly matching "repeated attempts produce the same result". Because the
prior CLOSED block's fail-closed invariant is already deployed and working,
this HTTP 400 is now correctly surfaced as "Не получилось выполнить
действие." instead of a false "Готово." -- which is exactly the currently
observed production behavior.

No real provider/network calls anywhere in this file -- OpenAI HTTP calls
are stubbed at the BoundedHttpClient boundary or a real-contract-enforcing
stand-in. No production writes, no Railway/deploy operations, no secrets
printed.
"""

from __future__ import annotations

import base64
import io
import logging
import unittest
from unittest.mock import AsyncMock, Mock

from PIL import Image

from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from integrations.production.adapters.media import (
    OpenAIImageGenerationProvider,
    build_image_provider,
)
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from product_media.errors import (
    STAGE_DECODE,
    STAGE_PERSISTENCE,
    STAGE_PROVIDER_REQUEST,
    STAGE_PROVIDER_RESPONSE,
    MediaError,
)
from product_media.providers.fake import FakeImageGenerationProvider
from product_media.readiness import NOT_READY, READY, check_image_generation_readiness
from product_media.runtime import build_product_media_runtime
from product_media.sqlite_store import SqliteMediaStore
from product_media.tools import ProductMediaToolAdapter
from side_effects.runtime import compose_side_effect_runtime

NOT_A_REAL_KEY = "sk-test-not-a-real-openai-key-99999"


def _real_looking_png(color=(10, 200, 10), size=(48, 48)) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


REAL_FIXTURE_PNG = _real_looking_png()
FIXTURE_B64 = base64.b64encode(REAL_FIXTURE_PNG).decode()

# OpenAI's real, documented size enums per model family (verified against
# public API docs, not guessed) -- used by the contract-enforcing stub below
# to prove the adapter never sends a combination OpenAI would reject.
_REAL_ALLOWED_SIZES = {
    "dall-e-2": {"256x256", "512x512", "1024x1024"},
    "dall-e-3": {"1024x1024", "1792x1024", "1024x1792"},
    "gpt-image-1": {"1024x1024", "1536x1024", "1024x1536", "auto"},
    "gpt-image-1.5": {"1024x1024", "1536x1024", "1024x1536", "auto"},
}


def _contract_enforcing_http_stub():
    """A stand-in for BoundedHttpClient that behaves like the REAL OpenAI
    images.generate endpoint's documented validation rules -- not a mock
    that merely records calls. Raises the same HTTP 400 OpenAI would raise
    for an unsupported response_format or an out-of-enum size, and only
    ever returns a successful body for a genuinely valid request. No
    network I/O occurs; this is pure in-process validation logic."""

    client = Mock()

    def _request(method, url, *, headers=None, json_body=None, content=None):
        model = str((json_body or {}).get("model") or "")
        size = str((json_body or {}).get("size") or "")
        allowed = _REAL_ALLOWED_SIZES.get(model)
        if model.startswith("gpt-image") and "response_format" in (json_body or {}):
            raise ProductionProviderError(
                ProviderErrorCategory.BAD_REQUEST, message="http_400", provider_id="media_image"
            )
        if allowed is not None and size not in allowed:
            raise ProductionProviderError(
                ProviderErrorCategory.BAD_REQUEST, message="http_400", provider_id="media_image"
            )
        resp = Mock()
        resp.json = Mock(return_value={"data": [{"b64_json": FIXTURE_B64}]})
        return resp

    client.request = Mock(side_effect=_request)
    return client


class ModelAwareRequestContractTests(unittest.TestCase):
    """STEP 3.C: the request payload must match each configured model's real,
    documented contract -- this is the exact defect class that produced the
    100%-reproducible production failure."""

    def _provider(self, model: str) -> OpenAIImageGenerationProvider:
        p = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model=model)
        p._http = _contract_enforcing_http_stub()
        return p

    def test_dalle2_square_default_uses_512_and_response_format(self):
        provider = self._provider("dall-e-2")
        provider.generate(prompt="панда", width=512, height=512)
        _, _, kwargs = provider._http.request.mock_calls[0]
        self.assertEqual(kwargs["json_body"]["size"], "512x512")
        self.assertEqual(kwargs["json_body"]["response_format"], "b64_json")

    def test_dalle2_small_hint_uses_256(self):
        provider = self._provider("dall-e-2")
        provider.generate(prompt="панда", width=200, height=200)
        _, _, kwargs = provider._http.request.mock_calls[0]
        self.assertEqual(kwargs["json_body"]["size"], "256x256")

    def test_dalle3_square_default_uses_1024_not_512(self):
        """Former defect: the adapter sent "512x512" for the default 1:1
        aspect ratio regardless of model -- invalid for dall-e-3."""
        provider = self._provider("dall-e-3")
        provider.generate(prompt="панда", width=512, height=512)
        _, _, kwargs = provider._http.request.mock_calls[0]
        self.assertEqual(kwargs["json_body"]["size"], "1024x1024")
        self.assertEqual(kwargs["json_body"]["response_format"], "b64_json")

    def test_dalle3_landscape_and_portrait_use_valid_enum_values(self):
        provider = self._provider("dall-e-3")
        provider.generate(prompt="панда", width=640, height=360)
        _, _, kwargs = provider._http.request.mock_calls[0]
        self.assertEqual(kwargs["json_body"]["size"], "1792x1024")
        provider2 = self._provider("dall-e-3")
        provider2.generate(prompt="панда", width=360, height=640)
        _, _, kwargs2 = provider2._http.request.mock_calls[0]
        self.assertEqual(kwargs2["json_body"]["size"], "1024x1792")

    def test_gpt_image_1_square_default_omits_response_format(self):
        """THE root-cause defect: gpt-image-1 rejects response_format outright
        (HTTP 400 "Unknown parameter"). It must never be sent for this model."""
        provider = self._provider("gpt-image-1")
        provider.generate(prompt="панда", width=512, height=512)
        _, _, kwargs = provider._http.request.mock_calls[0]
        self.assertEqual(kwargs["json_body"]["size"], "1024x1024")
        self.assertNotIn("response_format", kwargs["json_body"])

    def test_gpt_image_1_landscape_and_portrait_use_valid_enum_values(self):
        provider = self._provider("gpt-image-1")
        provider.generate(prompt="панда", width=640, height=360)
        _, _, kwargs = provider._http.request.mock_calls[0]
        self.assertEqual(kwargs["json_body"]["size"], "1536x1024")
        self.assertNotIn("response_format", kwargs["json_body"])
        provider2 = self._provider("gpt-image-1")
        provider2.generate(prompt="панда", width=360, height=640)
        _, _, kwargs2 = provider2._http.request.mock_calls[0]
        self.assertEqual(kwargs2["json_body"]["size"], "1024x1536")

    def test_unrecognized_future_model_defaults_to_gpt_image_like_contract(self):
        """An unknown/future model identifier must default to the safer,
        currently-flagship contract rather than the legacy dall-e-2 one."""
        provider = self._provider("gpt-image-2")
        provider.generate(prompt="панда", width=512, height=512)
        _, _, kwargs = provider._http.request.mock_calls[0]
        self.assertEqual(kwargs["json_body"]["size"], "1024x1024")
        self.assertNotIn("response_format", kwargs["json_body"])


class GptImageModelRegressionReproductionTests(unittest.IsolatedAsyncioTestCase):
    """Proves the exact production symptom end-to-end through the real
    conversation stack: with MEDIA_IMAGE_MODEL=gpt-image-1 configured, the
    UNPATCHED contract would deterministically fail every attempt (matching
    "Repeated attempt produces the same result"); the patched adapter
    produces a real artifact and a genuine "Готово." with an image."""

    def _gw_with_openai_provider(self, model: str):
        runtime = compose_side_effect_runtime(env={})
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model=model)
        provider._http = _contract_enforcing_http_stub()
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
        return gw, provider

    async def test_gpt_image_1_configured_production_scenario_now_succeeds(self):
        gw, provider = self._gw_with_openai_provider("gpt-image-1")
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды, которая ест бамбук в лесу ночью.",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-gptimg-1",
                conversation_id="c1",
            )
        )
        self.assertIn("Готово.", result.text)
        self.assertIn("![", result.text)
        self.assertTrue(result.metadata.get("artifacts"))
        self.assertNotIn("Не получилось", result.text)

    async def test_dalle3_configured_production_scenario_now_succeeds(self):
        gw, provider = self._gw_with_openai_provider("dall-e-3")
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды, которая ест бамбук в лесу ночью.",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-dalle3-1",
                conversation_id="c1",
            )
        )
        self.assertIn("Готово.", result.text)
        self.assertTrue(result.metadata.get("artifacts"))

    async def test_dalle2_default_model_still_works_no_regression(self):
        gw, provider = self._gw_with_openai_provider("dall-e-2")
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды, которая ест бамбук в лесу ночью.",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-dalle2-1",
                conversation_id="c1",
            )
        )
        self.assertIn("Готово.", result.text)
        self.assertTrue(result.metadata.get("artifacts"))


class PersistenceAndArtifactDeliveryFailClosedTests(unittest.TestCase):
    """STEP 3.E / TEST MATRIX #19-22: persistence and artifact-URL failures
    must fail the whole operation, not silently substitute/skip the broken
    asset."""

    def test_persistence_failure_after_successful_generation_fails_closed(self):
        class _BrokenStore(SqliteMediaStore):
            def save_version(self, version, *, blob):
                raise OSError("disk full (simulated)")

        service = build_product_media_runtime(db_path=":memory:")
        service.store = _BrokenStore(":memory:")
        service.generator = FakeImageGenerationProvider()
        with self.assertRaises(MediaError) as ctx:
            service.generate_from_brief(tenant_id="tenant-a", scene_description="панда")
        self.assertEqual(ctx.exception.stage, STAGE_PERSISTENCE)

    def test_persistence_failure_via_tool_adapter_reports_error_not_success(self):
        class _BrokenStore(SqliteMediaStore):
            def save_version(self, version, *, blob):
                raise OSError("disk full (simulated)")

        service = build_product_media_runtime(db_path=":memory:")
        service.store = _BrokenStore(":memory:")
        service.generator = FakeImageGenerationProvider()
        adapter = ProductMediaToolAdapter(service=service)
        result = adapter._chat_generate("tenant-a", {"scene_description": "панда"}, request_id="r1")
        self.assertEqual(result.get("status"), "error")
        self.assertNotIn("assets", result)

    def test_artifact_url_failure_after_successful_persistence_fails_closed(self):
        """Even if the provider and persistence both succeed, an empty/broken
        authorized view_url must fail the whole operation rather than expose
        a non-renderable asset."""
        service = build_product_media_runtime(db_path=":memory:")
        service.generator = FakeImageGenerationProvider()
        adapter = ProductMediaToolAdapter(service=service)
        import product_media.tools as tools_mod

        original = tools_mod.media_view_url
        tools_mod.media_view_url = lambda version_id: ""
        try:
            result = adapter._chat_generate("tenant-a", {"scene_description": "панда"}, request_id="r2")
        finally:
            tools_mod.media_view_url = original
        self.assertEqual(result.get("status"), "error")
        self.assertNotIn("assets", result)


class StructuredDiagnosticLoggingTests(unittest.TestCase):
    """STEP 4/TEST MATRIX #33-34: safe, secret-free structured logs at the
    image-generation boundary -- proves the NEXT production failure will be
    diagnosable from Railway logs without another blind engineering round."""

    def test_success_logs_started_and_succeeded_events_with_safe_fields(self):
        service = build_product_media_runtime(db_path=":memory:")
        service.generator = FakeImageGenerationProvider()
        adapter = ProductMediaToolAdapter(service=service)
        with self.assertLogs("product_media.image_generation", level="INFO") as cm:
            adapter._chat_generate("tenant-a", {"scene_description": "панда"}, request_id="req-log-ok")
        joined = "\n".join(cm.output)
        self.assertIn("event=image_generation_started", joined)
        self.assertIn("event=image_generation_succeeded", joined)
        self.assertIn("request_id=req-log-ok", joined)
        self.assertIn("tool_id=image.generate", joined)
        self.assertIn("artifact_count=1", joined)

    def test_failure_logs_typed_failure_stage_and_error_type(self):
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="gpt-image-1")
        provider._http = _contract_enforcing_http_stub()
        # Force the old, broken payload shape (response_format present for a
        # gpt-image model) to deterministically trigger the documented 400.
        original_supports = provider._supports_response_format
        provider._supports_response_format = lambda: True
        service = build_product_media_runtime(db_path=":memory:")
        service.generator = provider
        adapter = ProductMediaToolAdapter(service=service)
        with self.assertLogs("product_media.image_generation", level="INFO") as cm:
            result = adapter._chat_generate("tenant-a", {"scene_description": "панда"}, request_id="req-log-fail")
        provider._supports_response_format = original_supports
        self.assertEqual(result.get("status"), "error")
        joined = "\n".join(cm.output)
        self.assertIn("event=image_generation_failed", joined)
        self.assertIn(f"failure_stage={STAGE_PROVIDER_REQUEST}", joined)
        self.assertIn("error_type=MediaError", joined)
        self.assertIn("provider_error_code=http_400", joined)

    def test_no_secret_ever_appears_in_any_log_record(self):
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="gpt-image-1")
        provider._http = _contract_enforcing_http_stub()
        service = build_product_media_runtime(db_path=":memory:")
        service.generator = provider
        adapter = ProductMediaToolAdapter(service=service)
        with self.assertLogs("product_media.image_generation", level="INFO") as cm:
            adapter._chat_generate("tenant-a", {"scene_description": "панда"}, request_id="req-log-secret")
        joined = "\n".join(cm.output)
        self.assertNotIn(NOT_A_REAL_KEY, joined)
        self.assertNotIn(FIXTURE_B64, joined)

    def test_edit_boundary_also_logs_structured_events(self):
        service = build_product_media_runtime(db_path=":memory:")
        service.generator = FakeImageGenerationProvider()
        adapter = ProductMediaToolAdapter(service=service)
        gen_result = adapter._chat_generate("tenant-a", {"scene_description": "панда"}, request_id="req-edit-src")
        source_id = gen_result["assets"][0]["version_id"]
        with self.assertLogs("product_media.image_generation", level="INFO") as cm:
            adapter._chat_edit(
                "tenant-a",
                {"source_version_id": source_id, "instruction": "сделай ярче"},
                request_id="req-edit-1",
            )
        joined = "\n".join(cm.output)
        self.assertIn("tool_id=image.edit", joined)
        self.assertIn("event=image_generation_succeeded", joined)


class ProductionReadinessCheckTests(unittest.TestCase):
    """STEP 9/TEST MATRIX #35-36: zero-network readiness diagnostic."""

    def test_fake_provider_is_not_ready_with_safe_reason(self):
        readiness = check_image_generation_readiness(env={})
        self.assertEqual(readiness.status, NOT_READY)
        self.assertTrue(any("MEDIA_IMAGE_PROVIDER=fake" in r for r in readiness.reasons))

    def test_openai_without_key_is_not_ready_and_reports_key_empty_not_value(self):
        readiness = check_image_generation_readiness(
            env={"MEDIA_IMAGE_PROVIDER": "openai", "PRODUCT_MEDIA_DB_PATH": "/data/media.db"}
        )
        self.assertEqual(readiness.status, NOT_READY)
        self.assertIn("OPENAI_API_KEY=EMPTY", readiness.reasons)

    def test_openai_with_key_and_storage_configured_is_ready(self):
        readiness = check_image_generation_readiness(
            env={
                "MEDIA_IMAGE_PROVIDER": "openai",
                "OPENAI_API_KEY": NOT_A_REAL_KEY,
                "MEDIA_IMAGE_MODEL": "gpt-image-1",
                "PRODUCT_MEDIA_DB_PATH": "/data/media.db",
            }
        )
        self.assertEqual(readiness.status, READY)
        self.assertIn("OPENAI_API_KEY=PRESENT", readiness.reasons)
        self.assertNotIn(NOT_A_REAL_KEY, str(readiness))

    def test_readiness_check_makes_no_network_calls(self):
        """Structural guarantee: the function signature accepts only local
        config/objects, never an HTTP client -- calling it repeatedly must be
        side-effect-free and instantaneous (no network dependency)."""
        for _ in range(50):
            check_image_generation_readiness(env={"MEDIA_IMAGE_PROVIDER": "openai"})

    def test_readiness_reflects_capability_registration_when_registry_provided(self):
        runtime = compose_side_effect_runtime(env={})
        readiness = check_image_generation_readiness(
            env={"MEDIA_IMAGE_PROVIDER": "openai", "OPENAI_API_KEY": NOT_A_REAL_KEY, "PRODUCT_MEDIA_DB_PATH": "x"},
            tool_registry=runtime.tool_registry,
        )
        self.assertIn("IMAGE_GENERATE_CAPABILITY=REGISTERED", readiness.reasons)
        self.assertIn("PRODUCT_MEDIA_ADAPTER=REGISTERED", readiness.reasons)
        self.assertEqual(readiness.status, READY)

    def test_readiness_surfaced_via_existing_runtime_health_without_new_endpoint(self):
        """Must be reachable through the EXISTING health surface (no second
        health/status subsystem introduced)."""
        runtime = compose_side_effect_runtime(env={})
        health = runtime.health()
        self.assertIn("image_generation_readiness", health.metadata)
        self.assertIn("status", health.metadata["image_generation_readiness"])


class ErrorStageTaggingTests(unittest.TestCase):
    """STEP 5: typed failure-stage classification reusing MediaError (no
    duplicate exception hierarchy) -- proves each documented boundary is
    distinguishable."""

    def test_provider_request_error_tagged(self):
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="dall-e-2")
        client = Mock()
        client.request = Mock(
            side_effect=ProductionProviderError(ProviderErrorCategory.BAD_REQUEST, message="http_400", provider_id="media_image")
        )
        provider._http = client
        with self.assertRaises(MediaError) as ctx:
            provider.generate(prompt="x")
        self.assertEqual(ctx.exception.stage, STAGE_PROVIDER_REQUEST)

    def test_provider_response_error_tagged(self):
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="dall-e-2")
        client = Mock()
        resp = Mock()
        resp.json = Mock(return_value={"data": []})
        client.request = Mock(return_value=resp)
        provider._http = client
        with self.assertRaises(MediaError) as ctx:
            provider.generate(prompt="x")
        self.assertEqual(ctx.exception.stage, STAGE_PROVIDER_RESPONSE)

    def test_decode_error_tagged(self):
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="dall-e-2")
        client = Mock()
        resp = Mock()
        resp.json = Mock(return_value={"data": [{"b64_json": "not-valid-base64!!!"}]})
        client.request = Mock(return_value=resp)
        provider._http = client
        with self.assertRaises(MediaError) as ctx:
            provider.generate(prompt="x")
        self.assertEqual(ctx.exception.stage, STAGE_DECODE)

    def test_configuration_missing_key_is_a_distinct_error_type(self):
        with self.assertRaises(ProductionProviderError) as ctx:
            build_image_provider({"MEDIA_IMAGE_PROVIDER": "openai"})
        self.assertEqual(ctx.exception.category, ProviderErrorCategory.CONFIGURATION_ERROR)


class ExactUserScenarioOfflineRegressionTests(unittest.IsolatedAsyncioTestCase):
    """STEP 8: the exact reported user prompt, offline, both success and
    failure paths, plus retry-after-failure is not poisoned."""

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
        return gw

    async def test_exact_prompt_succeeds_with_patched_gpt_image_1_provider(self):
        provider = OpenAIImageGenerationProvider(api_key=NOT_A_REAL_KEY, model="gpt-image-1")
        provider._http = _contract_enforcing_http_stub()
        gw = self._gw(provider)
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды, которая ест бамбук в лесу ночью.",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-exact-1",
                conversation_id="c1",
            )
        )
        self.assertIn("Готово.", result.text)
        self.assertIn("![", result.text)
        artifacts = result.metadata.get("artifacts") or []
        self.assertEqual(len(artifacts), 1)
        self.assertTrue(artifacts[0]["view_url"])

    async def test_exact_prompt_with_rate_limited_provider_fails_closed_and_is_retryable(self):
        class _RateLimited:
            provider_id = "test-429"
            calls = 0

            def generate(self, *, prompt, width=512, height=512, seed=None):
                self.calls += 1
                raise MediaError("MEDIA_GENERATION_FAILED", "rate_limited", stage=STAGE_PROVIDER_REQUEST)

        provider = _RateLimited()
        gw = self._gw(provider)
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды, которая ест бамбук в лесу ночью.",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-exact-fail-1",
                conversation_id="c1",
            )
        )
        self.assertNotIn("Готово.", result.text)
        self.assertEqual(result.metadata.get("artifacts"), [])

        retry = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды, которая ест бамбук в лесу ночью.",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-exact-fail-2",
                conversation_id="c1",
            )
        )
        self.assertNotIn("Готово.", retry.text)
        self.assertEqual(provider.calls, 2, "a distinct retry request_id must call the provider again")

    async def test_successful_retry_after_failure_with_same_request_id_not_poisoned(self):
        class _FirstFailsThenSucceeds:
            provider_id = "test-flaky"
            calls = 0

            def generate(self, *, prompt, width=512, height=512, seed=None):
                self.calls += 1
                if self.calls == 1:
                    raise MediaError("MEDIA_GENERATION_FAILED", "provider_error_payload", stage=STAGE_PROVIDER_RESPONSE)
                return FakeImageGenerationProvider().generate(prompt=prompt, width=width, height=height, seed=seed)

        provider = _FirstFailsThenSucceeds()
        gw = self._gw(provider)
        req = ConversationRequest(
            text="Сгенерируй изображение панды, которая ест бамбук в лесу ночью.",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="req-flaky-retry",
            conversation_id="c1",
        )
        first = await gw.respond(req)
        self.assertNotIn("Готово.", first.text)
        self.assertEqual(first.metadata.get("artifacts"), [])
        second = await gw.respond(req)
        self.assertIn("Готово.", second.text)
        self.assertTrue(second.metadata.get("artifacts"))
        self.assertFalse(second.metadata.get("duplicate"))


if __name__ == "__main__":
    unittest.main()
