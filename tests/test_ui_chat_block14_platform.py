"""Block 14 — Voice / Multimodal / Chat UI closure tests."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from security.api_auth import configure_security
from security.auth import AuthService
from ui_chat.attachments import AttachmentRouter, AttachmentLimits, classify_attachment
from ui_chat.errors import UIChatError
from ui_chat.markdown import render_markdown_safe, sanitize_filename_display
from ui_chat.models import ATTACH_CLASS_IMAGE, ROLE_ASSISTANT, ROLE_USER, RUN_SUCCEEDED
from ui_chat.service import UIChatService
from ui_chat.sqlite_store import SqliteUIChatStore
from ui_chat.voice.stt import FakeSpeechToTextProvider
from ui_chat.voice.tts import FakeTextToSpeechProvider


def _auth_env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-a|tenant-a|user-a|user,operator|secret-a;"
            "key-b|tenant-b|user-b|user|secret-b"
        ),
        "UI_CHAT_MAX_UPLOAD_BYTES": str(512 * 1024),
        "SECURITY_MAX_REQUEST_BODY_BYTES": "65536",
    }


def _headers(tenant: str = "a") -> dict:
    return {"X-API-Key": f"secret-{tenant}"}


class _StubWorkflowEngine:
    last_workflow_id = "wf-test"

    async def execute(self, prompt, mode, role, **kwargs):
        return {
            "summary": f"Echo: {prompt[:80]}",
            "best_solution": f"Assistant reply to: {prompt[:60]}",
            "analysis": "",
            "confidence": 90,
        }


class _TinyPng:
    # minimal valid 1x1 PNG
    BYTES = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
    )


class UIChatServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        self.store = SqliteUIChatStore(self.tmp.name)
        self.engine = _StubWorkflowEngine()
        media = MagicMock()
        media.ingest.return_value = MagicMock(version_id="img-v1")
        self.svc = UIChatService(
            self.store,
            attachment_router=AttachmentRouter(
                limits=AttachmentLimits(max_file_bytes=1024 * 1024, max_attachments_per_turn=4),
                product_media_service=media,
            ),
            stt_provider=FakeSpeechToTextProvider(),
            tts_provider=FakeTextToSpeechProvider(max_chars=1000),
            workflow_engine=self.engine,
            run_router=MagicMock(),
        )

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    async def test_conversation_create_and_list(self):
        c = self.svc.create_conversation(tenant_id="tenant-a", user_id="user-a")
        items = self.svc.list_conversations(tenant_id="tenant-a", user_id="user-a")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].conversation_id, c.conversation_id)

    async def test_submit_turn_idempotency(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", user_id="user-a")
        r1 = await self.svc.submit_turn(
            tenant_id="tenant-a",
            user_id="user-a",
            conversation_id=conv.conversation_id,
            text="Hello",
            idempotency_key="idem-1",
            request_id="req-1",
            actor_ref="user-a",
        )
        r2 = await self.svc.submit_turn(
            tenant_id="tenant-a",
            user_id="user-a",
            conversation_id=conv.conversation_id,
            text="Hello",
            idempotency_key="idem-1",
            request_id="req-2",
            actor_ref="user-a",
        )
        self.assertEqual(r1.run_id, r2.run_id)
        msgs = self.svc.list_messages(
            tenant_id="tenant-a", user_id="user-a", conversation_id=conv.conversation_id
        )
        self.assertEqual(sum(1 for m in msgs if m.role == ROLE_USER), 1)

    async def test_empty_turn_rejected(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", user_id="user-a")
        with self.assertRaises(UIChatError):
            await self.svc.submit_turn(
                tenant_id="tenant-a",
                user_id="user-a",
                conversation_id=conv.conversation_id,
                text="",
                idempotency_key="idem-empty",
                request_id="req",
                actor_ref="user-a",
            )

    async def test_foreign_conversation_denied(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", user_id="user-a")
        with self.assertRaises(UIChatError):
            self.svc.get_conversation(
                tenant_id="tenant-b", user_id="user-b", conversation_id=conv.conversation_id
            )

    def test_voice_transcribe_and_edit_path(self):
        audio = b"PANDA_STT_TEST:Hello from voice"
        t = self.svc.transcribe_voice(tenant_id="tenant-a", user_id="user-a", audio=audio, mime_type="audio/wav")
        self.assertEqual(t.text, "Hello from voice")

    def test_voice_empty_rejected(self):
        with self.assertRaises(UIChatError):
            self.svc.transcribe_voice(tenant_id="tenant-a", user_id="user-a", audio=b"", mime_type="audio/wav")

    def test_voice_oversized_rejected(self):
        self.svc.max_voice_bytes = 10
        with self.assertRaises(UIChatError):
            self.svc.transcribe_voice(
                tenant_id="tenant-a", user_id="user-a", audio=b"x" * 20, mime_type="audio/wav"
            )

    async def test_tts_authorization_and_cache(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", user_id="user-a")
        run = await self.svc.submit_turn(
            tenant_id="tenant-a",
            user_id="user-a",
            conversation_id=conv.conversation_id,
            text="Hi",
            idempotency_key="idem-tts",
            request_id="req",
            actor_ref="user-a",
        )
        self.assertEqual(run.status, RUN_SUCCEEDED)
        msgs = self.svc.list_messages(
            tenant_id="tenant-a", user_id="user-a", conversation_id=conv.conversation_id
        )
        assistant = [m for m in msgs if m.role == ROLE_ASSISTANT][0]
        a1 = self.svc.synthesize_voice(
            tenant_id="tenant-a", user_id="user-a", message_id=assistant.message_id
        )
        a2 = self.svc.synthesize_voice(
            tenant_id="tenant-a", user_id="user-a", message_id=assistant.message_id
        )
        self.assertEqual(a1.artifact_id, a2.artifact_id)

    def test_tts_foreign_message_denied(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", user_id="user-a")
        msg_id = "fake-msg"
        with self.assertRaises(UIChatError):
            self.svc.synthesize_voice(tenant_id="tenant-b", user_id="user-b", message_id=msg_id)

    def test_image_upload_routes_block10(self):
        ref = self.svc.upload_attachment(
            tenant_id="tenant-a",
            user_id="user-a",
            conversation_id=None,
            filename="photo.png",
            mime_type="image/png",
            data=_TinyPng.BYTES,
        )
        self.assertEqual(ref.attachment_class, ATTACH_CLASS_IMAGE)
        self.assertEqual(ref.status, "READY")
        self.assertEqual(ref.artifact_ref, "img-v1")

    def test_foreign_attachment_denied(self):
        ref = self.svc.upload_attachment(
            tenant_id="tenant-a",
            user_id="user-a",
            conversation_id=None,
            filename="photo.png",
            mime_type="image/png",
            data=_TinyPng.BYTES,
        )
        with self.assertRaises(UIChatError):
            self.svc.get_attachment(tenant_id="tenant-b", user_id="user-b", attachment_id=ref.attachment_id)

    def test_background_task_list(self):
        task = self.svc._register_background_task(
            tenant_id="tenant-a",
            user_id="user-a",
            conversation_id=None,
            bg={"operation": "document_extract", "workflow_id": "wf-1"},
            attachment_id="att-1",
        )
        tasks = self.svc.list_tasks(tenant_id="tenant-a", user_id="user-a")
        self.assertEqual(tasks[0].task_id, task.task_id)

    def test_foreign_task_denied(self):
        task = self.svc._register_background_task(
            tenant_id="tenant-a",
            user_id="user-a",
            conversation_id=None,
            bg={"operation": "doc", "workflow_id": "wf-2"},
            attachment_id="att-2",
        )
        with self.assertRaises(UIChatError):
            self.svc.get_task(tenant_id="tenant-b", user_id="user-b", task_id=task.task_id)


class UIChatMarkdownSecurityTests(unittest.TestCase):
    def test_xss_script_escaped(self):
        html = render_markdown_safe('<script>alert(1)</script>')
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_xss_img_onerror(self):
        html_out = render_markdown_safe('<img src=x onerror=alert(1)>')
        self.assertIn("&lt;img", html_out)
        self.assertNotIn("<img", html_out)

    def test_javascript_link_blocked(self):
        html = render_markdown_safe("[click](javascript:alert(1))")
        self.assertNotIn("javascript:", html)

    def test_malicious_filename_sanitized(self):
        name = sanitize_filename_display('"><script>alert(1)</script>')
        self.assertNotIn("<script>", name)


class UIChatAttachmentTests(unittest.TestCase):
    def test_classify_image(self):
        self.assertEqual(classify_attachment("x.png", "image/png"), ATTACH_CLASS_IMAGE)

    def test_classify_spreadsheet(self):
        self.assertEqual(classify_attachment("data.csv", "text/csv"), "SPREADSHEET")

    def test_oversized_upload(self):
        router = AttachmentRouter(limits=AttachmentLimits(max_file_bytes=100))
        with self.assertRaises(UIChatError):
            router.validate_upload(data=b"x" * 200, filename="a.txt", mime_type="text/plain")


class UIChatHTTPTests(unittest.TestCase):
    def setUp(self):
        import importlib
        import main as main_mod

        os.environ.update(_auth_env())
        importlib.reload(main_mod)
        configure_security(auth=AuthService(env=_auth_env()))
        self.main = main_mod
        self.client = TestClient(main_mod.app)

    def _mock_execute(self):
        async def _exec(prompt, mode, role, **kwargs):
            return {"best_solution": "Hello back", "summary": "Hello back", "analysis": "", "confidence": 90}

        return patch.object(
            self.main.router.workflow_engine,
            "execute",
            new=AsyncMock(side_effect=_exec),
        )

    def test_e2e_basic_chat(self):
        with self._mock_execute():
            r = self.client.post("/api/chat/conversations", headers=_headers("a"))
            self.assertEqual(r.status_code, 200)
            conv_id = r.json()["conversation_id"]
            turn = self.client.post(
                f"/api/chat/conversations/{conv_id}/turns",
                headers=_headers("a"),
                json={"text": "Hello", "idempotency_key": "http-idem-1"},
            )
            self.assertEqual(turn.status_code, 200)
            msgs = self.client.get(
                f"/api/chat/conversations/{conv_id}/messages", headers=_headers("a")
            )
            self.assertEqual(msgs.status_code, 200)
            self.assertGreaterEqual(len(msgs.json()), 2)

    def test_home_serves_chat_ui(self):
        r = self.client.get("/app")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Panda", r.text)
        self.assertIn("/static/panda/js/app.js", r.text)
        legacy = self.client.get("/legacy-chat")
        self.assertEqual(legacy.status_code, 200)
        self.assertIn("Panda Chat", legacy.text)
        self.assertIn("/static/chat/chat.js", legacy.text)

    def test_static_chat_js_served(self):
        r = self.client.get("/static/chat/chat.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Panda Chat UI", r.text)

    def test_cross_tenant_conversation_denied(self):
        r = self.client.post("/api/chat/conversations", headers=_headers("a"))
        conv_id = r.json()["conversation_id"]
        r2 = self.client.get(f"/api/chat/conversations/{conv_id}", headers=_headers("b"))
        self.assertIn(r2.status_code, {403, 404, 422})

    def test_voice_transcribe_http(self):
        audio = b"PANDA_STT_TEST:HTTP voice"
        r = self.client.post(
            "/api/chat/voice/transcribe",
            headers=_headers("a"),
            files={"file": ("rec.webm", io.BytesIO(audio), "audio/webm")},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], "HTTP voice")

    def test_double_submit_idempotency_http(self):
        with self._mock_execute():
            conv = self.client.post("/api/chat/conversations", headers=_headers("a")).json()
            body = {"text": "Dup", "idempotency_key": "dup-key-99"}
            r1 = self.client.post(
                f"/api/chat/conversations/{conv['conversation_id']}/turns",
                headers=_headers("a"),
                json=body,
            )
            r2 = self.client.post(
                f"/api/chat/conversations/{conv['conversation_id']}/turns",
                headers=_headers("a"),
                json=body,
            )
            self.assertEqual(r1.json()["run_id"], r2.json()["run_id"])

    def test_document_no_public_api_still_passes(self):
        from evals.handlers import document_no_public_api

        class _Case:
            pass

        result = document_no_public_api(_Case())
        self.assertTrue(result.get("passed"), result)

    def test_no_api_key_in_static_js(self):
        r = self.client.get("/static/chat/chat.js")
        for needle in ("OPENAI_API_KEY", "Bearer sk-", "ANTHROPIC_API_KEY", "XAI_API_KEY"):
            self.assertNotIn(needle, r.text)


class UIChatVoiceProviderTests(unittest.TestCase):
    def test_stt_fake_provider(self):
        stt = FakeSpeechToTextProvider()
        text = stt.transcribe(audio=b"PANDA_STT_TEST:abc", mime_type="audio/wav")
        self.assertEqual(text, "abc")

    def test_tts_length_bound(self):
        tts = FakeTextToSpeechProvider(max_chars=10)
        with self.assertRaises(ValueError):
            tts.synthesize(text="x" * 20)


if __name__ == "__main__":
    unittest.main()
