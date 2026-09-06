"""PANDA MULTI-AGENT — Files/Artifacts/Attachments experience (Mega-Block 3.5).

Targeted, offline-safe coverage for the canonical artifact/file layer added
in this block: upload -> canonical registration -> authorized
metadata/view/download retrieval, tenant isolation, legacy-ref
compatibility, trusted agent/tool attachment resolution, and generated
image-artifact registration on top of the existing (unchanged)
product_media generation pipeline.

Does not reopen or re-audit any previously closed block. No live/paid
provider calls -- image generation uses the existing offline
``FakeImageGenerationProvider`` fixture already used by prior image-delivery
tests.
"""

from __future__ import annotations

import importlib
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from artifacts.errors import (
    ArtifactAccessDeniedError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    ArtifactTypeNotAllowedError,
)
from artifacts.models import KIND_DOCUMENT, KIND_IMAGE, KIND_PDF, KIND_SPREADSHEET, KIND_TEXT
from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore, SqliteArtifactStore
from business_assistant.action_continuation import ActionDecision, ActiveTask
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from product_media.providers.fake import FakeImageGenerationProvider
from side_effects.runtime import compose_side_effect_runtime
from tools.models import TOOL_STATUS_SUCCEEDED, ToolResult


def _auth_env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-a|tenant-a|user-a|user|secret-a;"
            "key-b|tenant-b|user-b|user|secret-b"
        ),
    }


def _headers(key: str) -> dict:
    return {"X-API-Key": key}


# --------------------------------------------------------------------------
# 3.5.1/3.5.12/3.5.14/3.5.15 -- canonical artifact layer, offline unit tests
# --------------------------------------------------------------------------
class ArtifactServiceCoreTests(unittest.TestCase):
    def _svc(self, media_provider=None):
        return ArtifactService(store=InMemoryArtifactStore(), media_provider=media_provider)

    def test_register_upload_classifies_kind_and_sniffs_mime(self):
        svc = self._svc()
        rec = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="report.pdf",
            content=b"%PDF-1.4\n%fake pdf body",
            mime_type="application/octet-stream",
        )
        self.assertEqual(rec.kind, KIND_PDF)
        self.assertEqual(rec.mime_type, "application/pdf")
        self.assertTrue(rec.artifact_id)

    def test_register_upload_rejects_disallowed_extension(self):
        svc = self._svc()
        with self.assertRaises(ArtifactTypeNotAllowedError):
            svc.register_upload(
                tenant_id="tenant-a",
                owner_id="user-a",
                filename="payload.exe",
                content=b"MZ\x90\x00fake",
                mime_type="application/octet-stream",
            )

    def test_register_upload_rejects_oversized_content(self):
        svc = self._svc()
        with self.assertRaises(ArtifactTooLargeError):
            svc.register_upload(
                tenant_id="tenant-a",
                owner_id="user-a",
                filename="big.txt",
                content=b"x" * (11 * 1024 * 1024),
                mime_type="text/plain",
            )

    def test_canonical_id_and_legacy_ref_both_resolve_to_same_record(self):
        svc = self._svc()
        rec = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="sheet.xlsx",
            content=b"PK\x03\x04fake-zip-body",
            mime_type="",
            legacy_ref="artifact://upload/upload-1/sheet.xlsx",
        )
        self.assertEqual(rec.kind, KIND_SPREADSHEET)
        by_canonical = svc.get_metadata(tenant_id="tenant-a", artifact_id=rec.artifact_id)
        by_legacy = svc.get_metadata(
            tenant_id="tenant-a", artifact_id="artifact://upload/upload-1/sheet.xlsx"
        )
        self.assertEqual(by_canonical.artifact_id, by_legacy.artifact_id)

    def test_tenant_isolation_denies_cross_tenant_metadata_and_blob(self):
        svc = self._svc()
        rec = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="notes.txt",
            content=b"private notes",
            mime_type="text/plain",
        )
        with self.assertRaises(ArtifactAccessDeniedError):
            svc.get_metadata(tenant_id="tenant-b", artifact_id=rec.artifact_id)
        with self.assertRaises(ArtifactAccessDeniedError):
            svc.get_blob(tenant_id="tenant-b", artifact_id=rec.artifact_id)

    def test_not_found_for_unknown_artifact_id(self):
        svc = self._svc()
        with self.assertRaises(ArtifactNotFoundError):
            svc.get_metadata(tenant_id="tenant-a", artifact_id="does-not-exist")

    def test_get_blob_resolves_legacy_ref_to_canonical_bytes(self):
        svc = self._svc()
        svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="doc.docx",
            content=b"PK\x03\x04docx-body",
            mime_type="",
            legacy_ref="artifact://upload/u2/doc.docx",
        )
        rec, blob = svc.get_blob(tenant_id="tenant-a", artifact_id="artifact://upload/u2/doc.docx")
        self.assertEqual(blob, b"PK\x03\x04docx-body")
        self.assertEqual(rec.kind, KIND_DOCUMENT)

    def test_sqlite_backend_roundtrip_matches_in_memory_backend(self):
        with tempfile.TemporaryDirectory() as d:
            svc = ArtifactService(store=SqliteArtifactStore(os.path.join(d, "a.sqlite3")))
            rec = svc.register_upload(
                tenant_id="tenant-a",
                owner_id="user-a",
                filename="plain.txt",
                content=b"hello world",
                mime_type="text/plain",
                legacy_ref="artifact://upload/u3/plain.txt",
            )
            self.assertEqual(rec.kind, KIND_TEXT)
            _, blob = svc.get_blob(tenant_id="tenant-a", artifact_id="artifact://upload/u3/plain.txt")
            self.assertEqual(blob, b"hello world")

    def test_register_generated_carries_provenance(self):
        svc = self._svc()
        src = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="source.csv",
            content=b"a,b\n1,2\n",
            mime_type="text/csv",
        )
        derived = svc.register_generated(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="source_cleaned.csv",
            content=b"a,b\n1,2\n",
            mime_type="text/csv",
            conversation_id="c1",
            request_id="req-1",
            tool_id="data.generate_excel",
            derived_from_artifact_id=src.artifact_id,
        )
        self.assertEqual(derived.derived_from_artifact_id, src.artifact_id)
        self.assertEqual(derived.source, "generated")

    def test_register_external_image_delegates_bytes_to_media_provider(self):
        media = Mock()
        media.get_blob = Mock(return_value=b"png-bytes-from-product-media")
        svc = self._svc(media_provider=media)
        rec = svc.register_external_image(
            tenant_id="tenant-a", owner_id="user-a", version_id="ver-123"
        )
        self.assertEqual(rec.kind, KIND_IMAGE)
        self.assertEqual(rec.size_bytes, 0)  # thin wrapper -- bytes not duplicated
        _, blob = svc.get_blob(tenant_id="tenant-a", artifact_id=rec.artifact_id)
        self.assertEqual(blob, b"png-bytes-from-product-media")
        media.get_blob.assert_called_once_with(tenant_id="tenant-a", version_id="ver-123")

    def test_resolve_trusted_ref_rejects_foreign_tenant_ref(self):
        svc = self._svc()
        rec = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="secret.pdf",
            content=b"%PDF-1.4 secret",
            mime_type="application/pdf",
        )
        # A malicious tenant-b request cannot resolve tenant-a's artifact_id.
        resolved = svc.resolve_trusted_ref(
            tenant_id="tenant-b", conversation_id="", ref=rec.artifact_id
        )
        self.assertIsNone(resolved)
        # The legitimate owner resolves it fine.
        own = svc.resolve_trusted_ref(tenant_id="tenant-a", conversation_id="", ref=rec.artifact_id)
        self.assertIsNotNone(own)
        self.assertEqual(own.artifact_id, rec.artifact_id)

    def test_soft_deleted_artifact_is_not_found_on_every_retrieval_path(self):
        svc = self._svc()
        rec = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="temp.txt",
            content=b"scratch data",
            mime_type="text/plain",
        )
        self.assertTrue(svc.delete_artifact(tenant_id="tenant-a", artifact_id=rec.artifact_id))
        with self.assertRaises(ArtifactNotFoundError):
            svc.get_metadata(tenant_id="tenant-a", artifact_id=rec.artifact_id)
        with self.assertRaises(ArtifactNotFoundError):
            svc.get_blob(tenant_id="tenant-a", artifact_id=rec.artifact_id)

    def test_delete_artifacts_for_conversation_cascades(self):
        svc = self._svc()
        rec1 = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="a.txt",
            content=b"a",
            mime_type="text/plain",
            conversation_id="c1",
        )
        rec2 = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="b.txt",
            content=b"b",
            mime_type="text/plain",
            conversation_id="c1",
        )
        other_conv = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="c.txt",
            content=b"c",
            mime_type="text/plain",
            conversation_id="c2",
        )
        count = svc.delete_artifacts_for_conversation(tenant_id="tenant-a", conversation_id="c1")
        self.assertEqual(count, 2)
        for rid in (rec1.artifact_id, rec2.artifact_id):
            with self.assertRaises(ArtifactNotFoundError):
                svc.get_metadata(tenant_id="tenant-a", artifact_id=rid)
        # Untouched: different conversation's artifact stays active.
        still_active = svc.get_metadata(tenant_id="tenant-a", artifact_id=other_conv.artifact_id)
        self.assertEqual(still_active.status, "active")

    def test_resolve_trusted_refs_filters_out_unresolvable_refs(self):
        svc = self._svc()
        rec = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="a.txt",
            content=b"hi",
            mime_type="text/plain",
        )
        out = svc.resolve_trusted_refs(
            tenant_id="tenant-a",
            conversation_id="",
            refs=(rec.artifact_id, "not-a-real-ref", ""),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["artifact_id"], rec.artifact_id)


# --------------------------------------------------------------------------
# 3.5.4 -- trusted agent/tool file access (conversation_gateway wiring)
# --------------------------------------------------------------------------
class TrustedAttachmentToolWiringTests(unittest.IsolatedAsyncioTestCase):
    def _gw_and_gateway(self, artifact_service):
        captured = {}

        async def _invoke(tool_request, capabilities=None):
            captured["tool_request"] = tool_request
            return ToolResult(
                request_id=tool_request.request_id,
                tool_id=tool_request.tool_id,
                operation=tool_request.operation,
                status=TOOL_STATUS_SUCCEEDED,
                success=True,
                data={},
            )

        fake_tool_gateway = Mock()
        fake_tool_gateway.invoke = AsyncMock(side_effect=_invoke)
        gw = WorkflowPandaConversationGateway(
            workflow_engine=Mock(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=fake_tool_gateway,
            artifact_service=artifact_service,
        )
        return gw, captured

    async def test_invoke_tool_injects_trusted_descriptor_for_owned_attachment(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        rec = svc.register_upload(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="prices.csv",
            content=b"sku,price\n1,10\n",
            mime_type="text/csv",
        )
        gw, captured = self._gw_and_gateway(svc)
        task = ActiveTask(
            task_id="t1",
            tenant_id="tenant-a",
            owner_id="user-a",
            conversation_id="c1",
            family="generic_tool",
            tool_id="some.tool",
            operation="run",
            goal="run tool",
        )
        action = ActionDecision(
            decision="CALL_TOOL",
            readiness="ready",
            continuation="none",
            task=task,
            arguments={},
            tool_id="some.tool",
            operation="run",
        )
        request = ConversationRequest(
            text="проверь мой файл",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="req-1",
            conversation_id="c1",
            attachment_refs=(rec.artifact_id,),
        )
        await gw._invoke_tool(request, action)
        sent_args = captured["tool_request"].arguments
        self.assertIn("attachment_refs", sent_args)
        self.assertEqual(len(sent_args["attachment_refs"]), 1)
        self.assertEqual(sent_args["attachment_refs"][0]["artifact_id"], rec.artifact_id)
        self.assertEqual(sent_args["attachment_refs"][0]["filename"], "prices.csv")

    async def test_invoke_tool_never_leaks_foreign_tenant_attachment(self):
        svc = ArtifactService(store=InMemoryArtifactStore())
        rec = svc.register_upload(
            tenant_id="tenant-b",
            owner_id="user-b",
            filename="confidential.pdf",
            content=b"%PDF-1.4 confidential",
            mime_type="application/pdf",
        )
        gw, captured = self._gw_and_gateway(svc)
        task = ActiveTask(
            task_id="t2",
            tenant_id="tenant-a",
            owner_id="user-a",
            conversation_id="c1",
            family="generic_tool",
            tool_id="some.tool",
            operation="run",
            goal="run tool",
        )
        action = ActionDecision(
            decision="CALL_TOOL",
            readiness="ready",
            continuation="none",
            task=task,
            arguments={},
            tool_id="some.tool",
            operation="run",
        )
        # tenant-a's request "attaches" tenant-b's artifact_id -- a spoof
        # attempt (e.g. guessed/leaked id, or a stale/forged client value).
        request = ConversationRequest(
            text="покажи содержимое файла",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="req-2",
            conversation_id="c1",
            attachment_refs=(rec.artifact_id,),
        )
        await gw._invoke_tool(request, action)
        sent_args = captured["tool_request"].arguments
        self.assertEqual(sent_args.get("attachment_refs"), [])


# --------------------------------------------------------------------------
# 3.5.5 -- generated image artifact registration (thin wrapper, no byte dup)
# --------------------------------------------------------------------------
class GeneratedImageArtifactRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_image_generation_registers_canonical_artifact(self):
        runtime = compose_side_effect_runtime(env={})
        runtime.product_media_service.generator = FakeImageGenerationProvider()
        artifact_service = ArtifactService(
            store=InMemoryArtifactStore(), media_provider=runtime.product_media_service
        )
        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "unused"})
        engine.last_workflow_id = "wf-1"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
            artifact_service=artifact_service,
        )
        result = await gw.respond(
            ConversationRequest(
                text="Сгенерируй изображение панды, которая ест бамбук",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="req-img-1",
                conversation_id="c1",
            )
        )
        # Pre-existing image delivery behavior is unchanged.
        self.assertIn("Готово.", result.text)
        self.assertIn("![", result.text)
        artifacts = result.metadata.get("artifacts") or []
        self.assertTrue(artifacts)
        image_artifacts = [a for a in artifacts if a.get("artifact_type") == "image"]
        self.assertTrue(image_artifacts)
        # New: the same image is now also reachable through the canonical
        # artifact layer without duplicating any bytes.
        canonical_id = image_artifacts[0].get("artifact_id")
        self.assertTrue(canonical_id)
        rec, blob = artifact_service.get_blob(tenant_id="tenant-a", artifact_id=canonical_id)
        self.assertEqual(rec.kind, KIND_IMAGE)
        self.assertTrue(blob)
        version_id = image_artifacts[0].get("ref")
        direct_blob = runtime.product_media_service.get_blob(
            tenant_id="tenant-a", version_id=version_id
        )
        self.assertEqual(blob, direct_blob)


# --------------------------------------------------------------------------
# 3.5.2/3.5.6/3.5.7/3.5.14 -- full HTTP round trip through the real app
# --------------------------------------------------------------------------
class ArtifactHttpRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "ba_api_http.sqlite")
        os.environ["BA_API_DB_PATH"] = cls.db
        os.environ["BA_API_UPLOAD_DIR"] = os.path.join(cls.tmp, "uploads")
        os.environ["PANDA_ARTIFACT_ROOT"] = os.path.join(cls.tmp, "artifacts")
        for k, v in _auth_env().items():
            os.environ[k] = v
        import main as main_mod

        cls.main = importlib.reload(main_mod)
        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _upload(self, key, filename, content, mime):
        return self.client.post(
            "/api/v1/business-assistant/attachments",
            headers=_headers(key),
            files={"file": (filename, content, mime)},
        )

    def test_upload_then_metadata_view_download_roundtrip(self):
        r = self._upload("secret-a", "quarterly.pdf", b"%PDF-1.4\nreport body", "application/pdf")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["artifact_id"])
        self.assertEqual(body["kind"], "pdf")
        self.assertTrue(body["view_url"].endswith("/view"))
        self.assertTrue(body["download_url"].endswith("/download"))
        artifact_id = body["artifact_id"]

        meta = self.client.get(
            f"/api/v1/business-assistant/artifacts/{artifact_id}", headers=_headers("secret-a")
        )
        self.assertEqual(meta.status_code, 200, meta.text)
        meta_body = meta.json()
        self.assertEqual(meta_body["filename"], "quarterly.pdf")
        self.assertEqual(meta_body["kind"], "pdf")
        self.assertEqual(meta_body["mime_type"], "application/pdf")

        dl = self.client.get(
            f"/api/v1/business-assistant/artifacts/{artifact_id}/download",
            headers=_headers("secret-a"),
        )
        self.assertEqual(dl.status_code, 200, dl.text)
        self.assertIn("attachment", dl.headers.get("content-disposition", ""))
        self.assertEqual(dl.content, b"%PDF-1.4\nreport body")

        view = self.client.get(
            f"/api/v1/business-assistant/artifacts/{artifact_id}/view",
            headers=_headers("secret-a"),
        )
        self.assertEqual(view.status_code, 200, view.text)
        self.assertIn("inline", view.headers.get("content-disposition", ""))

    def test_legacy_ref_resolves_same_resource_as_canonical_id(self):
        r = self._upload("secret-a", "notes.txt", b"hello notes", "text/plain")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        legacy_ref = body["artifact_ref"]
        import urllib.parse

        encoded = urllib.parse.quote(legacy_ref, safe="")
        meta = self.client.get(
            f"/api/v1/business-assistant/artifacts/{encoded}", headers=_headers("secret-a")
        )
        self.assertEqual(meta.status_code, 200, meta.text)
        self.assertEqual(meta.json()["artifact_id"], body["artifact_id"])

    def test_cross_tenant_access_is_denied(self):
        r = self._upload("secret-a", "private.csv", b"a,b\n1,2\n", "text/csv")
        self.assertEqual(r.status_code, 200, r.text)
        artifact_id = r.json()["artifact_id"]

        denied = self.client.get(
            f"/api/v1/business-assistant/artifacts/{artifact_id}", headers=_headers("secret-b")
        )
        self.assertIn(denied.status_code, {403, 404})

        denied_dl = self.client.get(
            f"/api/v1/business-assistant/artifacts/{artifact_id}/download",
            headers=_headers("secret-b"),
        )
        self.assertIn(denied_dl.status_code, {403, 404})

    def test_disallowed_extension_rejected(self):
        r = self._upload("secret-a", "malware.exe", b"MZ\x90\x00", "application/octet-stream")
        self.assertEqual(r.status_code, 422, r.text)

    def test_unknown_artifact_id_is_not_found(self):
        r = self.client.get(
            "/api/v1/business-assistant/artifacts/does-not-exist", headers=_headers("secret-a")
        )
        self.assertEqual(r.status_code, 404)

    def test_conversation_deletion_cascades_to_attached_artifact(self):
        conv = self.client.post(
            "/api/v1/business-assistant/conversations",
            headers=_headers("secret-a"),
            json={"title": "with attachment"},
        ).json()
        up = self._upload("secret-a", "attached.csv", b"a,b\n1,2\n", "text/csv")
        self.assertEqual(up.status_code, 200, up.text)
        legacy_ref = up.json()["artifact_ref"]
        artifact_id = up.json()["artifact_id"]

        # _submit_prepare() persists the message + attaches the artifact to
        # the conversation synchronously, before the (separately failing,
        # baseline-unconfigured-in-this-test-app) conversation gateway call
        # -- so the 503 that follows does not affect what we assert here.
        self.client.post(
            "/api/v1/business-assistant/requests",
            headers=_headers("secret-a"),
            json={
                "message": "проверь этот файл",
                "conversation_id": conv["conversation_id"],
                "artifact_refs": [legacy_ref],
                "idempotency_key": "cascade-delete-1",
            },
        )
        still_active = self.client.get(
            f"/api/v1/business-assistant/artifacts/{artifact_id}", headers=_headers("secret-a")
        )
        self.assertEqual(still_active.status_code, 200, still_active.text)

        delc = self.client.delete(
            f"/api/v1/business-assistant/conversations/{conv['conversation_id']}",
            headers=_headers("secret-a"),
        )
        self.assertEqual(delc.status_code, 204, delc.text)

        after_delete = self.client.get(
            f"/api/v1/business-assistant/artifacts/{artifact_id}", headers=_headers("secret-a")
        )
        self.assertEqual(after_delete.status_code, 404)

    def test_spreadsheet_and_document_uploads_are_classified_and_downloadable(self):
        xlsx = self._upload("secret-a", "prices.xlsx", b"PK\x03\x04fake-xlsx", "")
        self.assertEqual(xlsx.status_code, 200, xlsx.text)
        self.assertEqual(xlsx.json()["kind"], "spreadsheet")

        docx = self._upload("secret-a", "brief.docx", b"PK\x03\x04fake-docx", "")
        self.assertEqual(docx.status_code, 200, docx.text)
        self.assertEqual(docx.json()["kind"], "document")

        for body in (xlsx.json(), docx.json()):
            dl = self.client.get(
                f"/api/v1/business-assistant/artifacts/{body['artifact_id']}/download",
                headers=_headers("secret-a"),
            )
            self.assertEqual(dl.status_code, 200)


if __name__ == "__main__":
    unittest.main()
