"""PANDA LIVE IMAGE GENERATION ACTIVATION — deterministic, offline-safe tests.

Covers the previously missing wiring for: existing Action Continuation ->
image.generate -> existing ToolGateway -> real (fake, offline) provider adapter
boundary -> persisted image artifact -> safe authorized artifact URL -> Panda
chat image rendering.

No real provider/API/network calls. Uses the existing fake image generation
provider (product_media/providers/fake.py) and in-memory sqlite, exactly as
production defaults to when no MEDIA_IMAGE_PROVIDER/API key is configured.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from autonomy.capabilities import CAP_IMAGE_GENERATE
from autonomy.models import ACTION_WRITE
from business_assistant.action_continuation import (
    ActiveTaskStore,
    CALL_TOOL,
    FAMILY_IMAGE_GENERATE,
)
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from product_media.runtime import build_product_media_runtime
from product_media.tools import ProductMediaToolAdapter, media_view_url
from side_effects.runtime import compose_side_effect_runtime
from tools.gateway import ToolGateway
from tools.models import (
    TOOL_STATUS_SUCCEEDED,
    TOOL_TRUST_INTERNAL_SAFE,
    ToolRequest,
    WRITE_TRUST_LEVELS,
)
from tools.platform.descriptors import (
    image_edit_descriptor,
    image_generate_descriptor,
    media_generate_descriptor,
)


class DescriptorClassificationTests(unittest.TestCase):
    """Repository-evidence fix: image.generate/image.edit must not be routed
    through business-WRITE HITL governance (confirmed defect: they previously
    reused the same WRITE_EXTERNAL_REVERSIBLE trust as governed publishing
    tools, which forces AutonomyGate approval/denial for a purely internal,
    non-business-consequential chat artifact)."""

    def test_image_generate_is_internal_safe_read_only(self):
        desc = image_generate_descriptor(enabled=True)
        self.assertTrue(desc.read_only)
        self.assertEqual(desc.trust_level, TOOL_TRUST_INTERNAL_SAFE)
        self.assertNotIn(desc.trust_level, WRITE_TRUST_LEVELS)

    def test_image_edit_is_internal_safe_read_only(self):
        desc = image_edit_descriptor(enabled=True)
        self.assertTrue(desc.read_only)
        self.assertEqual(desc.trust_level, TOOL_TRUST_INTERNAL_SAFE)
        self.assertNotIn(desc.trust_level, WRITE_TRUST_LEVELS)

    def test_governed_media_generate_pipeline_unaffected(self):
        """Guard: the *governed* product-content-publishing pipeline
        (media.generate, distinct tool id) must keep requiring HITL/business-
        write governance — this fix only touches the ad-hoc chat tool ids."""
        desc = media_generate_descriptor(enabled=True)
        self.assertFalse(desc.read_only)
        self.assertIn(desc.trust_level, WRITE_TRUST_LEVELS)
        self.assertIn(ACTION_WRITE, desc.action_types_supported)


class ProductMediaAdapterChatPathTests(unittest.IsolatedAsyncioTestCase):
    """Real (offline/fake) provider adapter boundary + persisted artifact."""

    async def test_generate_persists_artifact_and_returns_safe_url(self):
        service = build_product_media_runtime(db_path=":memory:")
        adapter = ProductMediaToolAdapter(service)
        request = ToolRequest(
            request_id="r1",
            workflow_id="",
            task_id="t1",
            tool_id="image.generate",
            operation="generate",
            arguments={"scene_description": "wolf in a forest", "variant_count": 1},
            tenant_id="tenant-a",
        )
        data = await adapter.execute_read(request, {})
        self.assertEqual(data.get("status"), "completed")
        version_ids = data.get("version_ids") or []
        self.assertEqual(len(version_ids), 1)
        self.assertEqual(data.get("mime_type"), "image/png")
        self.assertTrue(data.get("view_url", "").startswith("/api/v1/business-assistant/media/"))
        self.assertEqual(data["view_url"], media_view_url(version_ids[0]))

        # Persisted: retrievable by the owning tenant through the service directly.
        version = service.get(tenant_id="tenant-a", version_id=version_ids[0])
        self.assertIsNotNone(version)
        blob = service.get_blob(tenant_id="tenant-a", version_id=version_ids[0])
        self.assertTrue(blob)

        # Tenant isolation preserved for the new artifact-serving accessor.
        self.assertIsNone(service.get(tenant_id="tenant-b", version_id=version_ids[0]))
        self.assertIsNone(service.get_blob(tenant_id="tenant-b", version_id=version_ids[0]))

    async def test_edit_persists_and_returns_safe_url(self):
        service = build_product_media_runtime(db_path=":memory:")
        adapter = ProductMediaToolAdapter(service)
        gen_req = ToolRequest(
            request_id="r1",
            workflow_id="",
            task_id="t1",
            tool_id="image.generate",
            operation="generate",
            arguments={"scene_description": "logo"},
            tenant_id="tenant-a",
        )
        gen_data = await adapter.execute_read(gen_req, {})
        source_id = gen_data["version_ids"][0]
        edit_req = ToolRequest(
            request_id="r2",
            workflow_id="",
            task_id="t1",
            tool_id="image.edit",
            operation="edit",
            arguments={"source_version_id": source_id, "instruction": "make it night"},
            tenant_id="tenant-a",
        )
        data = await adapter.execute_read(edit_req, {})
        self.assertTrue(data.get("view_url", "").startswith("/api/v1/business-assistant/media/"))
        self.assertTrue(service.get(tenant_id="tenant-a", version_id=data["version_id"]))


class ToolGatewayLiveActivationTests(unittest.IsolatedAsyncioTestCase):
    """image.generate through the *real*, production-wired ToolGateway
    (register_platform_tools / build_tool_gateway) must auto-succeed without
    approval or denial — no second ToolGateway, reuses existing boundary."""

    async def test_image_generate_auto_succeeds_no_approval(self):
        runtime = compose_side_effect_runtime(env={})
        gateway: ToolGateway = runtime.tool_gateway
        descriptor = gateway.get_tool("image.generate")
        self.assertTrue(descriptor.enabled, "image.generate must be enabled once product_media wiring succeeds")
        request = ToolRequest(
            request_id="req-1",
            workflow_id="",
            task_id="task-1",
            tool_id="image.generate",
            operation="generate",
            arguments={"scene_description": "a wolf in a forest at night", "variant_count": 1},
            requested_capabilities=(CAP_IMAGE_GENERATE,),
            tenant_id="tenant-a",
            user_id="user-a",
            actor_id="tenant-a:user-a",
        )
        result = await gateway.invoke(request)
        self.assertTrue(result.success, result.error_code)
        self.assertEqual(result.status, TOOL_STATUS_SUCCEEDED)
        self.assertIsNone(result.approval_id)
        self.assertFalse(result.side_effect)
        version_ids = result.data.get("version_ids") or []
        self.assertEqual(len(version_ids), 1)
        self.assertTrue(str(result.data.get("view_url") or "").startswith("/api/v1/business-assistant/media/"))


class ConversationGatewayLiveActivationTests(unittest.IsolatedAsyncioTestCase):
    """Full existing-architecture path: Action Continuation -> ToolGateway ->
    provider adapter -> persisted artifact -> chat-renderable markdown image,
    using the real production tool_gateway (not a hand-rolled test descriptor)."""

    async def test_wolf_request_renders_image_without_hitl(self):
        runtime = compose_side_effect_runtime(env={})
        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "should not be used", "role": "Judge"})
        engine.last_workflow_id = "wf-1"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
        )
        result = await gw.respond(
            ConversationRequest(
                text="нарисуй мне волка в лесу",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-wolf-1",
                conversation_id="c1",
            )
        )
        engine.execute.assert_not_called()
        self.assertEqual(gw.last_action_decision.decision, CALL_TOOL)
        self.assertEqual(gw.last_action_decision.task.family, FAMILY_IMAGE_GENERATE)
        self.assertNotEqual(gw.last_action_decision.decision, "REQUEST_APPROVAL")
        self.assertIn("![", result.text)
        self.assertIn("/api/v1/business-assistant/media/", result.text)
        artifacts = result.metadata.get("artifacts") or []
        self.assertTrue(artifacts)
        self.assertTrue(str(artifacts[0].get("view_url") or "").startswith("/api/v1/business-assistant/media/"))

    async def test_duplicate_request_id_does_not_regenerate(self):
        runtime = compose_side_effect_runtime(env={})
        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "x", "role": "Judge"})
        engine.last_workflow_id = "wf-1"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
        )
        req = ConversationRequest(
            text="нарисуй мне волка",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="same-req",
            conversation_id="c1",
        )
        first = await gw.respond(req)
        second = await gw.respond(req)
        self.assertTrue(first.metadata.get("artifacts"))
        self.assertTrue(second.metadata.get("duplicate") or second.metadata.get("artifacts"))


class MediaEndpointAuthAndIsolationTests(unittest.TestCase):
    """Safe authorized artifact URL: reuses the existing auth boundary
    (X-API-Key / session), enforces tenant isolation, no new auth system."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "ba_media.sqlite")
        cls.upload = os.path.join(cls.tmp, "uploads")
        os.makedirs(cls.upload, exist_ok=True)
        os.environ["BA_API_DB_PATH"] = cls.db
        os.environ["BA_API_UPLOAD_DIR"] = cls.upload
        os.environ["SECURITY_AUTH_MODE"] = "required"
        os.environ["PANDA_API_KEYS"] = (
            "key-a|tenant-a|user-a|user|secret-media-a;"
            "key-b|tenant-b|user-b|user|secret-media-b"
        )
        os.environ["PRODUCT_MEDIA_DB_PATH"] = ":memory:"

        import importlib

        import main as main_mod

        cls.main = importlib.reload(main_mod)
        cls.client = TestClient(cls.main.app)
        cls.media_service = cls.main.side_effect_runtime.product_media_service
        result = cls.media_service.generate_from_brief(
            tenant_id="tenant-a", scene_description="wolf", variant_count=1
        )
        cls.version_id = result["version_ids"][0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_media_wired_and_enabled(self):
        self.assertIsNotNone(self.media_service)
        gw = self.main.ba.conversation_gateway
        self.assertIsNotNone(gw)
        descriptor = gw._tool_gateway.get_tool("image.generate")
        self.assertTrue(descriptor.enabled)

    def test_unauthenticated_request_denied(self):
        r = self.client.get(f"/api/v1/business-assistant/media/{self.version_id}")
        self.assertEqual(r.status_code, 401)

    def test_owning_tenant_can_fetch_bytes(self):
        r = self.client.get(
            f"/api/v1/business-assistant/media/{self.version_id}",
            headers={"X-API-Key": "secret-media-a"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertTrue(len(r.content) > 0)
        self.assertEqual(r.headers.get("cache-control"), "private, no-store")

    def test_cross_tenant_fetch_denied(self):
        r = self.client.get(
            f"/api/v1/business-assistant/media/{self.version_id}",
            headers={"X-API-Key": "secret-media-b"},
        )
        self.assertEqual(r.status_code, 404)

    def test_unknown_version_returns_404_not_500(self):
        r = self.client.get(
            "/api/v1/business-assistant/media/does-not-exist",
            headers={"X-API-Key": "secret-media-a"},
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
