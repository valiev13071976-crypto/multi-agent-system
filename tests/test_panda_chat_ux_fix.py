"""Focused local tests — Panda chat management, canonical final answer, conversation layout.

No real provider/Telegram/network calls. Uses in-process fakes and static contracts.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from business_assistant.conversation_gateway import (
    ConversationRequest,
    FakePandaConversationGateway,
    WorkflowPandaConversationGateway,
    extract_assistant_text,
    is_internal_assistant_text,
    select_canonical_final_answer,
)
from business_assistant_api.errors import BAA_INVALID_REQUEST, BAA_NOT_FOUND, BusinessAssistantApiError
from business_assistant_api.router import configure_business_assistant_api_router
from business_assistant_api.runtime import build_business_assistant_api_runtime
from business_assistant_api.titles import (
    derive_auto_title,
    normalize_conversation_title,
    title_metadata_for_create,
    user_title_update,
)
from security.api_auth import configure_security
from security.auth import AuthService

ROOT = Path(__file__).resolve().parents[1]

JUDGE_SYNTHESIS = (
    "Синтез ответов экспертов без скрытого приоритета provider. "
    "Внешняя проверка фактов учитывается только при независимых источниках."
)
JUDGE_SUMMARY = "Финальный анализ успешно сформирован."
EXPERT_REPLY = "Здравствуйте. Чем могу помочь по работе?"


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _auth_env() -> dict:
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-a|tenant-a|user-a|user|secret-a;"
            "key-b|tenant-b|user-b|approver|secret-b"
        ),
    }


def _headers(secret: str) -> dict:
    return {"X-API-Key": secret}


class TitleHelperTests(unittest.TestCase):
    def test_trim_and_max_length(self):
        self.assertEqual(normalize_conversation_title("  привет   мир  "), "привет мир")
        long = "a" * 120
        self.assertEqual(len(normalize_conversation_title(long)), 80)

    def test_empty_rejected(self):
        self.assertEqual(normalize_conversation_title("   "), "")
        self.assertEqual(user_title_update("\n\t").get("title"), "")

    def test_default_create_is_placeholder(self):
        meta = title_metadata_for_create("Новый чат")
        self.assertEqual(meta["title_source"], "default")
        self.assertEqual(meta["title"], "Новый чат")

    def test_deterministic_first_message_title(self):
        self.assertEqual(derive_auto_title("  Нужен отчёт по складу  "), "Нужен отчёт по складу")
        self.assertEqual(derive_auto_title("привет"), derive_auto_title("привет"))


class CanonicalFinalAnswerTests(unittest.TestCase):
    def test_judge_synthesis_is_internal(self):
        self.assertTrue(is_internal_assistant_text(JUDGE_SYNTHESIS))
        self.assertTrue(is_internal_assistant_text(JUDGE_SUMMARY))

    def test_internal_synthesis_not_selected(self):
        selected = extract_assistant_text(
            {
                "summary": JUDGE_SUMMARY,
                "best_solution": JUDGE_SYNTHESIS,
                "analysis": f"openai: {EXPERT_REPLY}",
            }
        )
        self.assertEqual(selected, EXPERT_REPLY)
        self.assertNotIn("Синтез ответов экспертов", selected)
        self.assertNotIn("provider", selected)

    def test_canonical_prefers_explicit_final_answer(self):
        out = select_canonical_final_answer(
            {
                "final_answer": EXPERT_REPLY,
                "best_solution": JUDGE_SYNTHESIS,
                "summary": JUDGE_SUMMARY,
            }
        )
        self.assertEqual(out, EXPERT_REPLY)

    def test_missing_final_answer_is_empty(self):
        self.assertEqual(
            select_canonical_final_answer(
                {"summary": JUDGE_SUMMARY, "best_solution": JUDGE_SYNTHESIS, "analysis": ""}
            ),
            "",
        )

    def test_gateway_does_not_return_judge_metadata(self):
        class _Engine:
            last_workflow_id = "wf-local"

            async def execute(self, *args, **kwargs):
                return {
                    "summary": JUDGE_SUMMARY,
                    "best_solution": JUDGE_SYNTHESIS,
                    "analysis": f"fixture-expert: {EXPERT_REPLY}",
                    "role": "Judge",
                }

        gw = WorkflowPandaConversationGateway(
            workflow_engine=_Engine(),
            run_router=object(),
            context_manager=object(),
        )

        async def _run():
            return await gw.respond(
                ConversationRequest(
                    text="привет",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    request_id="req-1",
                )
            )

        result = asyncio.run(_run())
        self.assertEqual(result.text, EXPERT_REPLY)
        self.assertNotEqual(result.text, JUDGE_SYNTHESIS)


class ConversationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "ba.sqlite"),
            conversation_gateway=FakePandaConversationGateway(response=EXPERT_REPLY),
        )
        self.svc = self.rt.service

    def tearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rename_success_and_persistence(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a", title="Новый чат")
        renamed = self.svc.rename_conversation(
            tenant_id="tenant-a",
            owner_id="user-a",
            conversation_id=conv.conversation_id,
            title="  Рабочий чат  ",
        )
        self.assertEqual(renamed.metadata["title"], "Рабочий чат")
        listed = self.svc.list_conversations(tenant_id="tenant-a", owner_id="user-a")
        self.assertEqual(listed[0]["title"], "Рабочий чат")

    def test_delete_success_and_persistence(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a")
        self.svc.delete_conversation(
            tenant_id="tenant-a", owner_id="user-a", conversation_id=conv.conversation_id
        )
        listed = self.svc.list_conversations(tenant_id="tenant-a", owner_id="user-a")
        self.assertEqual(listed, [])

    def test_nonexistent_chat(self):
        with self.assertRaises(BusinessAssistantApiError) as ctx:
            self.svc.rename_conversation(
                tenant_id="tenant-a", owner_id="user-a", conversation_id="missing", title="X"
            )
        self.assertEqual(ctx.exception.code, BAA_NOT_FOUND)
        with self.assertRaises(BusinessAssistantApiError) as ctx:
            self.svc.delete_conversation(
                tenant_id="tenant-a", owner_id="user-a", conversation_id="missing"
            )
        self.assertEqual(ctx.exception.code, BAA_NOT_FOUND)

    def test_cross_tenant_rename_and_delete_denied(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a", title="Secret")
        with self.assertRaises(BusinessAssistantApiError) as ctx:
            self.svc.rename_conversation(
                tenant_id="tenant-b",
                owner_id="user-b",
                conversation_id=conv.conversation_id,
                title="Stolen",
            )
        self.assertEqual(ctx.exception.code, BAA_NOT_FOUND)
        with self.assertRaises(BusinessAssistantApiError) as ctx:
            self.svc.delete_conversation(
                tenant_id="tenant-b", owner_id="user-b", conversation_id=conv.conversation_id
            )
        self.assertEqual(ctx.exception.code, BAA_NOT_FOUND)
        listed = self.svc.list_conversations(tenant_id="tenant-a", owner_id="user-a")
        self.assertEqual(listed[0]["title"], "Secret")

    def test_empty_rename_rejected(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a")
        with self.assertRaises(BusinessAssistantApiError) as ctx:
            self.svc.rename_conversation(
                tenant_id="tenant-a",
                owner_id="user-a",
                conversation_id=conv.conversation_id,
                title="   ",
            )
        self.assertEqual(ctx.exception.code, BAA_INVALID_REQUEST)

    def test_auto_title_from_first_message(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a", title="Новый чат")
        self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message="  Нужен отчёт по складу  ",
            conversation_id=conv.conversation_id,
            idempotency_key="title-auto-1",
        )
        listed = self.svc.list_conversations(tenant_id="tenant-a", owner_id="user-a")
        self.assertEqual(listed[0]["title"], "Нужен отчёт по складу")

    def test_manual_rename_not_overwritten(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a", title="Новый чат")
        self.svc.rename_conversation(
            tenant_id="tenant-a",
            owner_id="user-a",
            conversation_id=conv.conversation_id,
            title="Мой чат",
        )
        self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message="второе сообщение которое не должно стать заголовком",
            conversation_id=conv.conversation_id,
            idempotency_key="title-manual-1",
        )
        listed = self.svc.list_conversations(tenant_id="tenant-a", owner_id="user-a")
        self.assertEqual(listed[0]["title"], "Мой чат")

    def test_title_xss_stored_as_text(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a")
        payload = '<img src=x onerror=alert(1)>'
        renamed = self.svc.rename_conversation(
            tenant_id="tenant-a",
            owner_id="user-a",
            conversation_id=conv.conversation_id,
            title=payload,
        )
        self.assertEqual(renamed.metadata["title"], payload)

    def test_canonical_final_answer_on_result(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a")
        rec = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message="привет",
            conversation_id=conv.conversation_id,
            idempotency_key="final-answer-1",
        )
        result = self.svc.get_result(
            tenant_id="tenant-a", owner_id="user-a", request_id=rec.request_id
        )
        self.assertEqual(result["final_answer"], EXPERT_REPLY)
        self.assertNotEqual(result["final_answer"], JUDGE_SYNTHESIS)
        msgs = self.svc.get_conversation_messages(
            tenant_id="tenant-a", owner_id="user-a", conversation_id=conv.conversation_id
        )
        assistant = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(assistant[-1]["content"], EXPERT_REPLY)


class ConversationHttpIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "ba-http.sqlite"),
            conversation_gateway=FakePandaConversationGateway(response=EXPERT_REPLY),
        )
        configure_security(auth=AuthService(env=_auth_env()))
        app = FastAPI()
        app.include_router(configure_business_assistant_api_router(self.rt.service))
        self.client = TestClient(app)

    def tearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unauthenticated_mutation_denied(self):
        r = self.client.patch("/api/v1/business-assistant/conversations/x", json={"title": "n"})
        self.assertIn(r.status_code, {401, 403})
        r = self.client.delete("/api/v1/business-assistant/conversations/x")
        self.assertIn(r.status_code, {401, 403})

    def test_http_rename_delete_and_cross_tenant(self):
        created = self.client.post(
            "/api/v1/business-assistant/conversations",
            headers=_headers("secret-a"),
            json={"title": "Новый чат"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        cid = created.json()["conversation_id"]
        renamed = self.client.patch(
            f"/api/v1/business-assistant/conversations/{cid}",
            headers=_headers("secret-a"),
            json={"title": "Проект А"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["title"], "Проект А")
        stolen = self.client.patch(
            f"/api/v1/business-assistant/conversations/{cid}",
            headers=_headers("secret-b"),
            json={"title": "nope"},
        )
        self.assertEqual(stolen.status_code, 404)
        stolen_del = self.client.delete(
            f"/api/v1/business-assistant/conversations/{cid}",
            headers=_headers("secret-b"),
        )
        self.assertEqual(stolen_del.status_code, 404)
        deleted = self.client.delete(
            f"/api/v1/business-assistant/conversations/{cid}",
            headers=_headers("secret-a"),
        )
        self.assertEqual(deleted.status_code, 204)
        missing = self.client.delete(
            f"/api/v1/business-assistant/conversations/{cid}",
            headers=_headers("secret-a"),
        )
        self.assertEqual(missing.status_code, 404)


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("static", "panda", "index.html")
        cls.css = _read("static", "panda", "panda.css")
        cls.theme = _read("static", "shared", "theme.css")
        cls.app = _read("static", "panda", "js", "app.js")
        cls.api = _read("static", "panda", "js", "api-client.js")
        cls.components = _read("static", "panda", "js", "components.js")
        cls.presentation = _read("static", "shared", "presentation.js")
        cls.sanitize = _read("static", "panda", "js", "sanitize.js")

    def test_chat_menu_rename_delete_confirm(self):
        self.assertIn("conv-menu-btn", self.components)
        self.assertIn("Переименовать", self.components)
        self.assertIn("Удалить", self.components)
        self.assertIn("renameConversation", self.api)
        self.assertIn("deleteConversation", self.api)
        self.assertIn("showDeleteConfirm", self.app)
        self.assertIn("chat-confirm-dialog", self.html)
        self.assertIn("startRename", self.app)
        self.assertIn("performDelete", self.app)
        self.assertIn('e.key === "Escape"', self.app)
        self.assertIn("pointerdown", self.app)
        self.assertIn("openMenuId", self.app)

    def test_title_xss_uses_textcontent(self):
        self.assertIn("setText", self.components)
        self.assertIn("el.textContent", self.sanitize)
        self.assertNotIn("innerHTML = conv.title", self.components)
        self.assertNotIn("innerHTML = renamed.title", self.app)

    def test_canonical_final_answer_frontend(self):
        self.assertIn("selectCanonicalFinalAnswer", self.presentation)
        self.assertIn("isInternalMetadata", self.presentation)
        self.assertIn("final_answer", self.app)
        self.assertIn("MISSING_FINAL_ANSWER", self.app)
        self.assertIn("синтез ответов экспертов без скрытого приоритета", self.presentation)

    def test_conversation_geometry(self):
        self.assertIn("has-messages", self.css)
        self.assertIn("justify-content: flex-end", self.css)
        self.assertIn("chat-scroll.has-messages", self.css)
        self.assertIn("min-height: 100%", self.css)
        self.assertIn("padding-bottom: 1.75rem", self.css)

    def test_composer_enter_ime(self):
        self.assertIn("isComposing", self.app)
        self.assertIn('e.key === "Enter" && !e.shiftKey', self.app)
        self.assertIn("autoGrowComposer", self.app)
        self.assertIn("min-height: 44px", self.css)
        self.assertIn("max-height: min(40vh, 240px)", self.css)

    def test_mobile_breakpoints(self):
        for bp in ("320px", "360px", "375px", "390px", "412px", "430px", "767px", "1023px"):
            self.assertTrue(bp in self.css or bp in self.theme, bp)
        self.assertIn("overflow-x: hidden", self.css)
        self.assertIn("white-space: pre", self.css)
        self.assertIn("sidebar-backdrop", self.html)

    def test_session_logout_preserved(self):
        self.assertIn("/api/accounts/logout", self.app)
        self.assertIn("hasHumanSession", self.app)
        self.assertIn("hasHumanSession", self.api)
        self.assertIn('id="owner-nav-link"', self.html)


if __name__ == "__main__":
    unittest.main()
