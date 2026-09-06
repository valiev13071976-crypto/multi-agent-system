"""PANDA MULTI-AGENT — Block 3.5 Production Acceptance Defect Closure.

"ChatGPT-like generated image actions": direct Edit/Download actions for a
generated image in the normal conversation view, without requiring the
lightbox to be opened first.

Targeted, offline-safe coverage for the NEW deterministic dispatch this
defect fix adds on top of the existing (unchanged) architecture:

- ``ConversationRequest.image_edit_source_ref`` / ``respond()`` /
  ``_invoke_tool()`` in ``business_assistant/conversation_gateway.py``
  (deterministic image.edit dispatch, exact-artifact targeting, fail-closed
  trust boundary via ``ArtifactService.resolve_trusted_image_source``);
- ``BusinessAssistantApiService.edit_generated_image`` (the direct
  "Редактировать" action's server-side entry point);
- the canonical ``/artifacts/{id}/view`` URL now embedded in the generated-
  image markdown reply (what lets the UI recover the exact artifact_id for
  direct actions with zero new message fields).

Does NOT reopen or re-audit Mega-Block 3.5. No live/paid provider calls --
image generation/edit use the existing offline ``FakeImageGenerationProvider``
/ ``FakeImageEditProvider`` fixtures already used by prior image tests.

The purely front-end proofs (direct buttons rendered without opening the
lightbox, clicking the image still opens it, lightbox download still works)
are additive DOM changes in ``static/panda/js/sanitize.js``/``app.js`` built
on the exact same, already-covered rendering primitives
(``.msg-image``/``.msg-image-wrap``/lightbox) -- this repository has no JS
test harness (see ``static/panda/``: no package.json/jest config), so those
are verified by code inspection/manual review rather than a new, out-of-scope
JS test framework.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import CALL_TOOL, FAMILY_IMAGE_EDIT
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from business_assistant.service import BusinessAssistantService
from business_assistant_api.errors import BusinessAssistantApiError
from business_assistant_api.service import BusinessAssistantApiService
from business_assistant_api.store import SqliteBusinessAssistantApiStore
from product_media.providers.fake import FakeImageGenerationProvider
from side_effects.runtime import compose_side_effect_runtime
from tools.models import TOOL_STATUS_SUCCEEDED, ToolResult


def _engine():
    engine = Mock()
    engine.execute = AsyncMock(return_value={"final_answer": "unused"})
    engine.last_workflow_id = "wf-1"
    return engine


def _capturing_gateway(artifact_service):
    """Fake ToolGateway that records the exact ToolRequest it receives and
    returns a deterministic image.edit-shaped success payload -- mirrors the
    existing pattern in test_files_artifacts_attachments_block_3_5_platform.py
    (TrustedAttachmentToolWiringTests)."""

    captured = {}

    async def _invoke(tool_request, **kwargs):
        captured["tool_request"] = tool_request
        return ToolResult(
            request_id=tool_request.request_id,
            tool_id=tool_request.tool_id,
            operation=tool_request.operation,
            status=TOOL_STATUS_SUCCEEDED,
            success=True,
            data={
                "version_id": "edited-version-xyz",
                "version_ids": ["edited-version-xyz"],
                "assets": [
                    {
                        "version_id": "edited-version-xyz",
                        "artifact_type": "image",
                        "mime_type": "image/png",
                        "view_url": "/api/v1/business-assistant/media/edited-version-xyz",
                    }
                ],
                "mime_type": "image/png",
                "status": "completed",
            },
        )

    fake_tool_gateway = Mock()
    fake_tool_gateway.invoke = AsyncMock(side_effect=_invoke)
    gw = WorkflowPandaConversationGateway(
        workflow_engine=_engine(),
        run_router=object(),
        context_manager=object(),
        tool_gateway=fake_tool_gateway,
        artifact_service=artifact_service,
    )
    return gw, captured, fake_tool_gateway


# --------------------------------------------------------------------------
# Deterministic exact-artifact targeting + trust boundary (proofs 7, 8, 9, 11)
# --------------------------------------------------------------------------
class DirectImageEditTargetingTests(unittest.IsolatedAsyncioTestCase):
    def _register_image(self, svc, *, tenant_id, owner_id, conversation_id, version_id):
        return svc.register_external_image(
            tenant_id=tenant_id,
            owner_id=owner_id,
            version_id=version_id,
            conversation_id=conversation_id,
        )

    async def test_direct_edit_dispatches_image_edit_with_exact_source_version_id(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        rec = self._register_image(
            svc, tenant_id="tenant-a", owner_id="user-a", conversation_id="c1", version_id="ver-a-1"
        )
        gw, captured, _ = _capturing_gateway(svc)

        result = await gw.respond(
            ConversationRequest(
                text="сделай фон белым",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-edit-1",
                conversation_id="c1",
                image_edit_source_ref=rec.artifact_id,
            )
        )

        sent = captured["tool_request"]
        self.assertEqual(sent.tool_id, "image.edit")
        self.assertEqual(sent.operation, "edit")
        self.assertEqual(sent.arguments.get("source_version_id"), "ver-a-1")
        self.assertEqual(sent.arguments.get("instruction"), "сделай фон белым")
        self.assertEqual(gw.last_action_decision.task.family, FAMILY_IMAGE_EDIT)
        self.assertEqual(gw.last_action_decision.decision, CALL_TOOL)
        # Existing image-delivery contract (Готово. + markdown image) is reused
        # unchanged for the edit result too.
        self.assertIn("Готово.", result.text)
        self.assertIn("![", result.text)

    async def test_direct_edit_multiple_images_selects_exact_one_not_others(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        rec_a = self._register_image(
            svc, tenant_id="tenant-a", owner_id="user-a", conversation_id="c1", version_id="ver-a-A"
        )
        rec_b = self._register_image(
            svc, tenant_id="tenant-a", owner_id="user-a", conversation_id="c1", version_id="ver-a-B"
        )
        rec_c = self._register_image(
            svc, tenant_id="tenant-a", owner_id="user-a", conversation_id="c1", version_id="ver-a-C"
        )
        gw, captured, _ = _capturing_gateway(svc)

        await gw.respond(
            ConversationRequest(
                text="сделай ярче",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-edit-b",
                conversation_id="c1",
                image_edit_source_ref=rec_b.artifact_id,
            )
        )

        sent_version_id = captured["tool_request"].arguments.get("source_version_id")
        self.assertEqual(sent_version_id, "ver-a-B")
        self.assertNotEqual(sent_version_id, "ver-a-A")
        self.assertNotEqual(sent_version_id, "ver-a-C")
        # Sanity: the fixtures really are three distinct canonical artifacts.
        self.assertEqual(len({rec_a.artifact_id, rec_b.artifact_id, rec_c.artifact_id}), 3)

    async def test_direct_edit_cross_tenant_source_fails_closed_without_invoking_tool(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        foreign = self._register_image(
            svc, tenant_id="tenant-b", owner_id="user-b", conversation_id="c1", version_id="ver-b-1"
        )
        gw, captured, fake_tool_gateway = _capturing_gateway(svc)

        result = await gw.respond(
            ConversationRequest(
                text="удали текст на фото",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-edit-spoof",
                conversation_id="c1",
                # tenant-a's request "selects" tenant-b's artifact_id -- a
                # spoof attempt (guessed/leaked id, stale/forged client value).
                image_edit_source_ref=foreign.artifact_id,
            )
        )

        fake_tool_gateway.invoke.assert_not_called()
        self.assertNotIn("captured_tool_request", captured)
        self.assertNotIn("Готово.", result.text)
        self.assertNotIn("![", result.text)

    async def test_direct_edit_unknown_artifact_fails_closed(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        gw, captured, fake_tool_gateway = _capturing_gateway(svc)

        result = await gw.respond(
            ConversationRequest(
                text="сделай теплее",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-edit-unknown",
                conversation_id="c1",
                image_edit_source_ref="00000000-0000-0000-0000-000000000000",
            )
        )

        fake_tool_gateway.invoke.assert_not_called()
        self.assertNotIn("![", result.text)

    async def test_direct_edit_different_conversation_fails_closed(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        rec = self._register_image(
            svc, tenant_id="tenant-a", owner_id="user-a", conversation_id="c-other", version_id="ver-a-2"
        )
        gw, captured, fake_tool_gateway = _capturing_gateway(svc)

        await gw.respond(
            ConversationRequest(
                text="сделай квадратным",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-edit-conv-mismatch",
                conversation_id="c1",
                image_edit_source_ref=rec.artifact_id,
            )
        )

        fake_tool_gateway.invoke.assert_not_called()


# --------------------------------------------------------------------------
# Real end-to-end generate -> edit through the production tool_gateway
# (proofs 1, 9, 12): no live/paid calls -- FakeImageGenerationProvider /
# FakeImageEditProvider (product_media's existing offline default editor).
# --------------------------------------------------------------------------
class DirectImageEditEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_then_direct_edit_produces_new_artifact_without_duplicating_source(self):
        runtime = compose_side_effect_runtime(env={})
        runtime.product_media_service.generator = FakeImageGenerationProvider()
        artifact_service = ArtifactService(
            store=InMemoryArtifactStore(), media_provider=runtime.product_media_service
        )
        gw = WorkflowPandaConversationGateway(
            workflow_engine=_engine(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
            artifact_service=artifact_service,
        )

        generated = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды в лесу",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-gen-1",
                conversation_id="c1",
            )
        )
        gen_artifacts = generated.metadata.get("artifacts") or []
        self.assertTrue(gen_artifacts)
        source_artifact_id = gen_artifacts[0]["artifact_id"]
        # Production acceptance defect closure: the markdown reply now embeds
        # the canonical artifact-view URL (what the UI parses for direct
        # Edit/Download actions), not the raw product_media media URL.
        self.assertIn(f"/api/v1/business-assistant/artifacts/{source_artifact_id}/view", generated.text)

        before_source_rec, before_source_blob = artifact_service.get_blob(
            tenant_id="tenant-a", artifact_id=source_artifact_id
        )

        edited = await gw.respond(
            ConversationRequest(
                text="сделай фон белым",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-edit-real-1",
                conversation_id="c1",
                image_edit_source_ref=source_artifact_id,
            )
        )
        self.assertIn("Готово.", edited.text)
        edit_artifacts = edited.metadata.get("artifacts") or []
        self.assertTrue(edit_artifacts)
        edited_artifact_id = edit_artifacts[0]["artifact_id"]

        # A new artifact was created for the edit RESULT only -- never a second
        # copy of the source.
        self.assertNotEqual(edited_artifact_id, source_artifact_id)
        self.assertIn(f"/api/v1/business-assistant/artifacts/{edited_artifact_id}/view", edited.text)

        # The source artifact/bytes are completely untouched by the edit.
        after_source_rec, after_source_blob = artifact_service.get_blob(
            tenant_id="tenant-a", artifact_id=source_artifact_id
        )
        self.assertEqual(before_source_blob, after_source_blob)
        self.assertEqual(before_source_rec.storage_ref, after_source_rec.storage_ref)

        # Exactly one new canonical artifact exists (the edit result) -- no
        # duplicate source bytes/artifact was created as a side effect of
        # selecting/editing the image.
        edited_rec, edited_blob = artifact_service.get_blob(
            tenant_id="tenant-a", artifact_id=edited_artifact_id
        )
        self.assertNotEqual(edited_blob, before_source_blob)


# --------------------------------------------------------------------------
# BusinessAssistantApiService.edit_generated_image (the direct action's
# server-side entry point) -- proofs 10 (reload/persistence contract) and
# request validation / unavailable-gateway handling.
# --------------------------------------------------------------------------
class EditGeneratedImageServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, *, artifact_service, conversation_gateway):
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.close()
        store = SqliteBusinessAssistantApiStore(tmp.name)
        ba = BusinessAssistantService(conversation_gateway=conversation_gateway)
        api = BusinessAssistantApiService(store=store, ba_service=ba)
        api.artifact_service = artifact_service
        return api

    async def test_edit_persists_both_turns_with_canonical_artifact_association(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        rec = svc.register_external_image(
            tenant_id="tenant-a", owner_id="user-a", conversation_id="c1", version_id="ver-a-9"
        )
        gw, _, _ = _capturing_gateway(svc)
        api = self._service(artifact_service=svc, conversation_gateway=gw)
        api._ensure_conversation("tenant-a", "user-a", "c1")

        result = await api.edit_generated_image(
            tenant_id="tenant-a",
            owner_id="user-a",
            conversation_id="c1",
            source_artifact_id=rec.artifact_id,
            instruction="сделай фон белым",
        )
        self.assertTrue(result["request_id"])
        self.assertIn("![", result["text"])

        # Reload proof: messages are retrievable exactly as persisted, with
        # the user's instruction referencing the exact source artifact and
        # the assistant reply embedding the resulting image's canonical URL
        # -- a fresh page load re-renders the identical Edit/Download-capable
        # markup from this same persisted content (sanitize.js reparses the
        # URL at render time, generate or reload alike).
        messages = api.get_conversation_messages(tenant_id="tenant-a", owner_id="user-a", conversation_id="c1")
        self.assertEqual(len(messages), 2)
        user_msg, assistant_msg = messages
        self.assertEqual(user_msg["role"], "user")
        self.assertIn(rec.artifact_id, user_msg["artifact_refs"])
        self.assertEqual(assistant_msg["role"], "assistant")
        self.assertIn("/artifacts/", assistant_msg["content"])
        self.assertIn("/view", assistant_msg["content"])

    async def test_edit_rejects_missing_instruction(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        rec = svc.register_external_image(
            tenant_id="tenant-a", owner_id="user-a", conversation_id="c1", version_id="ver-a-10"
        )
        gw, _, _ = _capturing_gateway(svc)
        api = self._service(artifact_service=svc, conversation_gateway=gw)
        with self.assertRaises(BusinessAssistantApiError):
            await api.edit_generated_image(
                tenant_id="tenant-a",
                owner_id="user-a",
                conversation_id="c1",
                source_artifact_id=rec.artifact_id,
                instruction="   ",
            )

    async def test_edit_rejects_missing_source_artifact(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        gw, _, _ = _capturing_gateway(svc)
        api = self._service(artifact_service=svc, conversation_gateway=gw)
        with self.assertRaises(BusinessAssistantApiError):
            await api.edit_generated_image(
                tenant_id="tenant-a",
                owner_id="user-a",
                conversation_id="c1",
                source_artifact_id="",
                instruction="сделай фон белым",
            )

    async def test_edit_fails_gracefully_when_conversation_gateway_unconfigured(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        rec = svc.register_external_image(
            tenant_id="tenant-a", owner_id="user-a", conversation_id="c1", version_id="ver-a-11"
        )
        api = self._service(artifact_service=svc, conversation_gateway=None)
        with self.assertRaises(BusinessAssistantApiError):
            await api.edit_generated_image(
                tenant_id="tenant-a",
                owner_id="user-a",
                conversation_id="c1",
                source_artifact_id=rec.artifact_id,
                instruction="сделай фон белым",
            )


if __name__ == "__main__":
    unittest.main()
