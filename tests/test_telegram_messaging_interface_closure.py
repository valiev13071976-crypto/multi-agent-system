"""Telegram / Messaging Interface closure tests."""

from __future__ import annotations

import importlib
import io
import os
import shutil
import tempfile
import unittest
import uuid

from fastapi.testclient import TestClient

from business_assistant_api.models import ST_COMPLETED, ST_WAITING_FOR_APPROVAL
from business_assistant_api.runtime import build_business_assistant_api_runtime
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from telegram_interface.errors import TGI_ACCESS_DENIED, TGI_DUPLICATE_UPDATE, TelegramInterfaceError
from telegram_interface.render import escape_telegram_text
from telegram_interface.runtime import build_telegram_interface_runtime


def _env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "TELEGRAM_INTERFACE_ENABLED": "true",
        "TELEGRAM_ENABLED": "false",
        "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
        "PANDA_API_KEYS": (
            "key-a|tenant-a|user-a|user|secret-a;"
            "key-approver|tenant-a|approver-a|approver|secret-approver"
        ),
    }


def _active_integration(act: IntegrationActivationService, tenant: str, provider: str):
    ref = act.put_secret_ref(tenant_id=tenant, secret_ref=f"secret:{provider}", value=f"tok-{provider}")
    conn = act.configure_connection(
        tenant_id=tenant, provider_id=provider, credential_ref=ref, environment=ENV_FIXTURE
    )
    act.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    act.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)


def _msg_payload(update_id: str, chat_id: str, user_id: str, text: str) -> dict:
    return {
        "update_id": int(update_id) if update_id.isdigit() else update_id,
        "message": {
            "message_id": 1,
            "from": {"id": int(user_id), "is_bot": False, "first_name": "Test"},
            "chat": {"id": int(chat_id), "type": "private"},
            "text": text,
        },
    }


def _doc_payload(update_id: str, chat_id: str, user_id: str, file_id: str, filename: str) -> dict:
    return {
        "update_id": int(update_id),
        "message": {
            "message_id": 2,
            "from": {"id": int(user_id), "is_bot": False, "first_name": "Test"},
            "chat": {"id": int(chat_id), "type": "private"},
            "document": {
                "file_id": file_id,
                "file_name": filename,
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "file_size": 128,
            },
        },
    }


def _callback_payload(update_id: str, chat_id: str, user_id: str, data: str) -> dict:
    return {
        "update_id": int(update_id),
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": int(user_id), "is_bot": False, "first_name": "Test"},
            "data": data,
            "message": {"message_id": 3, "chat": {"id": int(chat_id), "type": "private"}},
        },
    }


class TelegramInterfaceUnitTests(unittest.TestCase):
    def test_escape_malicious_content(self):
        payload = '<script>alert(1)</script>'
        out = escape_telegram_text(payload)
        self.assertNotIn("<script>", out)

    def test_package_imports(self):
        import telegram_interface.service
        import telegram_interface.router

        self.assertTrue(hasattr(telegram_interface.service, "TelegramInterfaceService"))


class TelegramInterfaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ba_db = os.path.join(self.tmp, "ba.sqlite")
        self.tg_db = os.path.join(self.tmp, "tg.sqlite")
        self.upload = os.path.join(self.tmp, "uploads")
        os.makedirs(self.upload, exist_ok=True)
        env = {**_env(), "BA_API_DB_PATH": self.ba_db, "TELEGRAM_INTERFACE_DB_PATH": self.tg_db, "BA_API_UPLOAD_DIR": self.upload}
        self.ba_rt = build_business_assistant_api_runtime(db_path=self.ba_db, env=env)
        self.rt = build_telegram_interface_runtime(
            env=env, ba_api=self.ba_rt.service, db_path=self.tg_db, upload_dir=self.upload
        )
        self.svc = self.rt.service
        self.chat = "100001"
        self.user = "200001"
        self.binding = self.svc.register_binding(
            tenant_id="tenant-a",
            owner_id="approver-a",
            telegram_user_id=self.user,
            chat_id=self.chat,
        )

    def tearDown(self):
        self.rt.close()
        self.ba_rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _handle(self, payload: dict):
        return self.svc.handle_payload(tenant_id="tenant-a", payload=payload)

    def test_binding_required(self):
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self._handle(_msg_payload("9001", "999999", "888888", "hello"))
        self.assertEqual(ctx.exception.code, "tgi_binding_required")

    def test_simple_analysis_flow(self):
        out = self._handle(
            _msg_payload("1", self.chat, self.user, "Summarize quarterly revenue trends for leadership")
        )
        self.assertEqual(out["status"], "ok")
        self.assertIn("request_id", out)
        rec = self.ba_rt.service.get_request(
            tenant_id="tenant-a", owner_id="approver-a", request_id=out["request_id"]
        )
        self.assertEqual(rec.status, ST_COMPLETED)

    def test_duplicate_update(self):
        payload = _msg_payload("42", self.chat, self.user, "Summarize quarterly revenue trends")
        self._handle(payload)
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self._handle(payload)
        self.assertEqual(ctx.exception.code, TGI_DUPLICATE_UPDATE)

    def test_excel_attachment_batch(self):
        file_id = "file-excel-1"
        self.svc.transport.register_file_fixture(file_id, b"xlsx-bytes", "prices.xlsx")
        out = self._handle(
            _doc_payload("50", self.chat, self.user, file_id, "prices.xlsx")
        )
        self.assertEqual(out["status"], "ok")
        rec = self.ba_rt.service.get_request(
            tenant_id="tenant-a", owner_id="approver-a", request_id=out["request_id"]
        )
        self.assertEqual(rec.workload_class, "batch")

    def test_ozon_read(self):
        _active_integration(self.ba_rt.service.ba.integration_activation, "tenant-a", "ozon")
        out = self._handle(
            _msg_payload("60", self.chat, self.user, "Покажи текущие заказы на Ozon")
        )
        self.assertEqual(out["status"], "ok")

    def test_bitrix_governed_write_approve(self):
        self.ba_rt.service.ba.seed_supplier_fixture(
            rows=[{"sku": "S1", "brand": "Samsung", "title": "Phone", "price": "2000", "ambiguous": False}],
            costs={"S1": "1000"},
        )
        _active_integration(self.ba_rt.service.ba.integration_activation, "tenant-a", "bitrix")
        out = self._handle(
            _msg_payload("70", self.chat, self.user, "Опубликуй подготовленные товары Samsung на сайт Bitrix")
        )
        rec = self.ba_rt.service.get_request(
            tenant_id="tenant-a", owner_id="approver-a", request_id=out["request_id"]
        )
        self.assertEqual(rec.status, ST_WAITING_FOR_APPROVAL)
        token = self.rt.store._conn.execute(
            "SELECT token FROM tgi_callbacks WHERE action='approve' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        approve_payload = _callback_payload("71", self.chat, self.user, f"panda:{token}")
        out2 = self._handle(approve_payload)
        self.assertEqual(out2["status"], "ok")
        self.assertEqual(len(self.ba_rt.service.ba._external_writes), 1)

    def test_duplicate_approval(self):
        self.ba_rt.service.ba.seed_supplier_fixture(
            rows=[{"sku": "S2", "brand": "Samsung", "title": "Tab", "price": "3000", "ambiguous": False}],
            costs={"S2": "1500"},
        )
        _active_integration(self.ba_rt.service.ba.integration_activation, "tenant-a", "bitrix")
        out = self._handle(
            _msg_payload("80", self.chat, self.user, "Опубликуй товары Samsung на Bitrix")
        )
        token = self.rt.store._conn.execute(
            "SELECT token FROM tgi_callbacks WHERE action='approve' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        self._handle(_callback_payload("81", self.chat, self.user, f"panda:{token}"))
        dup = self._handle(_callback_payload("82", self.chat, self.user, f"panda:{token}"))
        self.assertEqual(dup["status"], "duplicate")
        self.assertEqual(len(self.ba_rt.service.ba._external_writes), 1)

    def test_reject(self):
        self.ba_rt.service.ba.seed_supplier_fixture(
            rows=[{"sku": "S3", "brand": "Samsung", "title": "Watch", "price": "900", "ambiguous": False}],
            costs={"S3": "400"},
        )
        out = self._handle(
            _msg_payload("90", self.chat, self.user, "Опубликуй товары Samsung на Bitrix")
        )
        rec = self.ba_rt.service.get_request(
            tenant_id="tenant-a", owner_id="approver-a", request_id=out["request_id"]
        )
        if rec.status == ST_WAITING_FOR_APPROVAL:
            token = self.rt.store._conn.execute(
                "SELECT token FROM tgi_callbacks WHERE request_id=? AND action='reject'",
                (out["request_id"],),
            ).fetchone()[0]
            self._handle(_callback_payload("91", self.chat, self.user, f"panda:{token}"))
            rec2 = self.ba_rt.service.get_request(
                tenant_id="tenant-a", owner_id="approver-a", request_id=out["request_id"]
            )
            self.assertEqual(rec2.status, "REJECTED")
            self.assertEqual(len(self.ba_rt.service.ba._external_writes), 0)

    def test_tenant_isolation(self):
        self.svc.register_binding(
            tenant_id="tenant-a",
            owner_id="user-b",
            telegram_user_id="300001",
            chat_id="100002",
        )
        out_a = self._handle(_msg_payload("101", self.chat, self.user, "Summarize quarterly revenue trends"))
        from business_assistant_api.errors import BusinessAssistantApiError

        with self.assertRaises(BusinessAssistantApiError):
            self.ba_rt.service.get_request(
                tenant_id="tenant-a", owner_id="user-b", request_id=out_a["request_id"]
            )

    def test_callback_tampering(self):
        token = uuid.uuid4().hex[:16]
        self.rt.store.save_callback(
            __import__("telegram_interface.models", fromlist=["CallbackToken"]).CallbackToken(
                token=token,
                tenant_id="tenant-a",
                owner_id="other-user",
                request_id="fake-req",
                action="approve",
                created_at="2020-01-01T00:00:00+00:00",
            )
        )
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self._handle(_callback_payload("110", self.chat, self.user, f"panda:{token}"))
        self.assertEqual(ctx.exception.code, TGI_ACCESS_DENIED)

    def test_unsupported_file(self):
        file_id = "bad-exe"
        self.svc.transport.register_file_fixture(file_id, b"MZ", "malware.exe")
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self._handle(_doc_payload("120", self.chat, self.user, file_id, "malware.exe"))
        self.assertEqual(ctx.exception.code, "tgi_file_unsupported")

    def test_cancel_command(self):
        out = self._handle(
            _msg_payload("140", self.chat, self.user, "Summarize quarterly revenue trends")
        )
        session = self.rt.store.get_session(self.chat)
        self.assertTrue(session.active_request_id or out.get("request_id"))
        req_id = out["request_id"]
        self._handle(_msg_payload("141", self.chat, self.user, "/cancel"))
        rec = self.ba_rt.service.get_request(tenant_id="tenant-a", owner_id="approver-a", request_id=req_id)
        self.assertIn(rec.status, {"CANCELLED", ST_COMPLETED})

    def test_restart_recovery(self):
        out = self._handle(
            _msg_payload("130", self.chat, self.user, "Summarize quarterly revenue trends for Q1")
        )
        req_id = out["request_id"]
        self.rt.close()
        rt2 = build_telegram_interface_runtime(
            env={**_env(), "BA_API_DB_PATH": self.ba_db, "TELEGRAM_INTERFACE_DB_PATH": self.tg_db, "BA_API_UPLOAD_DIR": self.upload},
            ba_api=self.ba_rt.service,
            db_path=self.tg_db,
            upload_dir=self.upload,
        )
        session = rt2.service.recover_session(self.chat)
        self.assertIsNotNone(session)
        rec = self.ba_rt.service.get_request(tenant_id="tenant-a", owner_id="approver-a", request_id=req_id)
        self.assertEqual(rec.status, ST_COMPLETED)
        rt2.close()


class TelegramInterfaceHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.ba_db = os.path.join(cls.tmp, "ba_http.sqlite")
        cls.tg_db = os.path.join(cls.tmp, "tg_http.sqlite")
        cls.upload = os.path.join(cls.tmp, "uploads")
        os.makedirs(cls.upload, exist_ok=True)
        for k, v in {**_env(), "BA_API_DB_PATH": cls.ba_db, "TELEGRAM_INTERFACE_DB_PATH": cls.tg_db, "BA_API_UPLOAD_DIR": cls.upload}.items():
            os.environ[k] = v
        import main as main_mod

        cls.main = importlib.reload(main_mod)
        cls.client = TestClient(cls.main.app)
        cls.tg = cls.main.tg_interface_runtime.service
        cls.tg.register_binding(
            tenant_id="tenant-a",
            owner_id="approver-a",
            telegram_user_id="200001",
            chat_id="100001",
        )

    @classmethod
    def tearDownClass(cls):
        if cls.main.tg_interface_runtime:
            cls.main.tg_interface_runtime.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_fixture_update_endpoint(self):
        r = self.client.post(
            "/api/v1/telegram/fixture/updates/tenant-a",
            json=_msg_payload("500", "100001", "200001", "Summarize quarterly revenue trends"),
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_webhook_verification(self):
        r = self.client.post(
            "/api/v1/telegram/webhook/tenant-a",
            json=_msg_payload("501", "100001", "200001", "hello"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        self.assertEqual(r.status_code, 401)

    def test_openapi_includes_telegram(self):
        spec = self.client.get("/openapi.json").json()
        self.assertIn("/api/v1/telegram/webhook/{tenant_id}", spec.get("paths", {}))


if __name__ == "__main__":
    unittest.main()
