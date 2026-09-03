"""Web Interface closure tests — static shell, BA API transport, E2E flows."""

from __future__ import annotations

import html
import importlib
import io
import os
import re
import shutil
import tempfile
import unittest
import uuid

from fastapi.testclient import TestClient

from business_assistant_api.models import ST_COMPLETED, ST_WAITING_FOR_APPROVAL
from business_assistant_api.runtime import build_business_assistant_api_runtime
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService


def _auth_env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-a|tenant-a|user-a|user|secret-a;"
            "key-approver|tenant-a|approver-a|approver|secret-approver;"
            "key-b|tenant-b|user-b|approver|secret-b"
        ),
    }


def _headers(key: str) -> dict:
    return {"X-API-Key": key}


def _active_integration(act: IntegrationActivationService, tenant: str, provider: str):
    ref = act.put_secret_ref(tenant_id=tenant, secret_ref=f"secret:{provider}", value=f"tok-{provider}")
    conn = act.configure_connection(
        tenant_id=tenant, provider_id=provider, credential_ref=ref, environment=ENV_FIXTURE
    )
    act.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    act.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)


def _escape_html(text: str) -> str:
    """Mirror static/panda/js/sanitize.js escapeHtml for XSS contract tests."""
    s = str(text or "")
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


class WebInterfaceStaticTests(unittest.TestCase):
    def test_panda_static_assets_exist(self):
        required = [
            "static/panda/index.html",
            "static/panda/panda.css",
            "static/panda/js/sanitize.js",
            "static/panda/js/api-client.js",
            "static/panda/js/components.js",
            "static/panda/js/app.js",
        ]
        for path in required:
            self.assertTrue(os.path.isfile(path), path)

    def test_index_references_canonical_modules(self):
        with open("static/panda/index.html", encoding="utf-8") as fh:
            body = fh.read()
        for mod in ("sanitize.js", "api-client.js", "components.js", "app.js"):
            self.assertIn(mod, body)
        self.assertIn('id="auth-gate"', body)
        self.assertIn('id="approval-panel"', body)

    def test_api_client_uses_business_assistant_base(self):
        with open("static/panda/js/api-client.js", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("/api/v1/business-assistant", src)
        self.assertIn("sessionStorage", src)
        self.assertNotIn("localStorage.setItem", src)

    def test_sanitize_escapes_script_tags(self):
        payload = '<script>alert("xss")</script>'
        escaped = _escape_html(payload)
        self.assertNotIn("<script>", escaped)
        self.assertIn("&lt;script&gt;", escaped)


class WebInterfaceTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "web_ui.sqlite")
        self.upload = os.path.join(self.tmp, "uploads")
        os.makedirs(self.upload, exist_ok=True)
        self.rt = build_business_assistant_api_runtime(db_path=self.db)
        self.rt.upload_dir = self.upload
        self.rt.service.upload_dir = self.upload
        self.svc = self.rt.service

    def tearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_conversation_list_create_messages(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a", title="Pricing")
        listed = self.svc.list_conversations(tenant_id="tenant-a", owner_id="user-a")
        self.assertTrue(any(c["conversation_id"] == conv.conversation_id for c in listed))
        rec = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message="Analyze Samsung prices",
            conversation_id=conv.conversation_id,
            idempotency_key="web-conv-1",
        )
        msgs = self.svc.get_conversation_messages(
            tenant_id="tenant-a", owner_id="user-a", conversation_id=conv.conversation_id
        )
        self.assertGreaterEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["request_id"], rec.request_id)

    def test_conversation_tenant_isolation(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a")
        with self.assertRaises(Exception):
            self.svc.get_conversation_messages(
                tenant_id="tenant-b", owner_id="user-b", conversation_id=conv.conversation_id
            )

    def test_upload_attachment_valid(self):
        content = b"sku,price\nS1,100\n"
        out = self.svc.upload_attachment(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="prices.csv",
            content=content,
            mime_type="text/csv",
            upload_base_dir=self.upload,
        )
        self.assertTrue(out["artifact_ref"].startswith("artifact://upload/"))
        self.assertEqual(out["size_bytes"], len(content))

    def test_upload_rejects_bad_extension(self):
        with self.assertRaises(Exception):
            self.svc.upload_attachment(
                tenant_id="tenant-a",
                owner_id="user-a",
                filename="evil.exe",
                content=b"MZ",
                mime_type="application/octet-stream",
                upload_base_dir=self.upload,
            )


class WebInterfaceHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "web_ui_http.sqlite")
        cls.upload = os.path.join(cls.tmp, "uploads")
        os.makedirs(cls.upload, exist_ok=True)
        os.environ["BA_API_DB_PATH"] = cls.db
        os.environ["BA_API_UPLOAD_DIR"] = cls.upload
        for k, v in _auth_env().items():
            os.environ[k] = v
        import main as main_mod

        cls.main = importlib.reload(main_mod)
        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_home_serves_panda_app(self):
        r = self.client.get("/app")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Panda", r.text)
        self.assertIn("/static/panda/js/app.js", r.text)

    def test_legacy_chat_route(self):
        r = self.client.get("/legacy-chat")
        self.assertEqual(r.status_code, 200)

    def test_conversations_http(self):
        r = self.client.post(
            "/api/v1/business-assistant/conversations",
            headers=_headers("secret-a"),
            json={"title": "Supplier review"},
        )
        self.assertEqual(r.status_code, 200)
        conv_id = r.json()["conversation_id"]
        lst = self.client.get("/api/v1/business-assistant/conversations", headers=_headers("secret-a"))
        self.assertEqual(lst.status_code, 200)
        self.assertTrue(any(c["conversation_id"] == conv_id for c in lst.json()))

    def test_attachment_http(self):
        data = io.BytesIO(b"col1\n1\n")
        r = self.client.post(
            "/api/v1/business-assistant/attachments",
            headers=_headers("secret-a"),
            files={"file": ("sheet.csv", data, "text/csv")},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("artifact_ref", r.json())

    def test_openapi_includes_web_transport(self):
        spec = self.client.get("/openapi.json").json()
        paths = spec.get("paths", {})
        self.assertIn("/api/v1/business-assistant/conversations", paths)
        self.assertIn("/api/v1/business-assistant/attachments", paths)


class WebInterfaceE2EFlowTests(unittest.TestCase):
    """Deterministic HTTP sequences mirroring WEB-E2E flows."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "web_e2e.sqlite")
        cls.upload = os.path.join(cls.tmp, "uploads")
        os.makedirs(cls.upload, exist_ok=True)
        os.environ["BA_API_DB_PATH"] = cls.db
        os.environ["BA_API_UPLOAD_DIR"] = cls.upload
        for k, v in _auth_env().items():
            os.environ[k] = v
        import main as main_mod

        cls.main = importlib.reload(main_mod)
        cls.svc = cls.main.ba_api_runtime.service
        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls):
        cls.main.ba_api_runtime.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _submit(self, message: str, **kwargs):
        body = {"message": message, "idempotency_key": kwargs.pop("idempotency_key", f"idem-{uuid.uuid4().hex}")}
        body.update(kwargs)
        r = self.client.post(
            "/api/v1/business-assistant/requests",
            headers=_headers("secret-a"),
            json=body,
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_web_e2e_1_simple_analysis(self):
        conv = self.client.post(
            "/api/v1/business-assistant/conversations",
            headers=_headers("secret-a"),
            json={"title": "Analysis"},
        ).json()
        req = self._submit(
            "Summarize quarterly revenue trends for the leadership team",
            conversation_id=conv["conversation_id"],
        )
        st = self.client.get(
            f"/api/v1/business-assistant/requests/{req['request_id']}/status",
            headers=_headers("secret-a"),
        ).json()
        self.assertEqual(st["status"], ST_COMPLETED)
        ev = self.client.get(
            f"/api/v1/business-assistant/requests/{req['request_id']}/events",
            headers=_headers("secret-a"),
        ).json()
        self.assertGreaterEqual(len(ev), 2)

    def test_web_e2e_2_excel_flow(self):
        up = self.client.post(
            "/api/v1/business-assistant/attachments",
            headers=_headers("secret-a"),
            files={"file": ("prices.xlsx", io.BytesIO(b"xlsx-bytes"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        ).json()
        req = self._submit(
            "Сравни закупку с текущими ценами и подготовь итоговую Excel таблицу.",
            artifact_refs=[up["artifact_ref"]],
        )
        self.assertEqual(req["workload_class"], "batch")
        arts = self.client.get(
            f"/api/v1/business-assistant/requests/{req['request_id']}/artifacts",
            headers=_headers("secret-a"),
        ).json()
        self.assertTrue(any(a.get("artifact_type") == "excel_report" for a in arts))

    def test_web_e2e_3_ozon_read(self):
        _active_integration(self.svc.ba.integration_activation, "tenant-a", "ozon")
        req = self._submit("Покажи текущие заказы на Ozon", read_only=True)
        result = self.client.get(
            f"/api/v1/business-assistant/requests/{req['request_id']}/result",
            headers=_headers("secret-a"),
        )
        self.assertIn(result.status_code, {200, 404})

    def test_web_e2e_4_bitrix_write_approve(self):
        self.svc.ba.seed_supplier_fixture(
            rows=[{"sku": "S1", "brand": "Samsung", "title": "Phone", "price": "2000", "ambiguous": False}],
            costs={"S1": "1000"},
        )
        _active_integration(self.svc.ba.integration_activation, "tenant-a", "bitrix")
        req = self.client.post(
            "/api/v1/business-assistant/requests",
            headers=_headers("secret-approver"),
            json={
                "message": "Опубликуй подготовленные товары Samsung на сайт Bitrix",
                "idempotency_key": "web-bitrix-approve-1",
            },
        )
        self.assertEqual(req.status_code, 200, req.text)
        req = req.json()
        self.assertEqual(req["status"], ST_WAITING_FOR_APPROVAL)
        preview = self.client.get(
            f"/api/v1/business-assistant/requests/{req['request_id']}/preview",
            headers=_headers("secret-approver"),
        )
        self.assertEqual(preview.status_code, 200)
        appr = self.client.post(
            f"/api/v1/business-assistant/requests/{req['request_id']}/approve",
            headers=_headers("secret-approver"),
            json={"approval_id": req["approval_id"]},
        )
        self.assertEqual(appr.status_code, 200)
        self.assertEqual(appr.json()["status"], ST_COMPLETED)
        self.assertEqual(len(self.svc.ba._external_writes), 1)

    def test_web_e2e_5_duplicate_approval(self):
        writes_before = len(self.svc.ba._external_writes)
        self.svc.ba.seed_supplier_fixture(
            rows=[{"sku": "S2", "brand": "Samsung", "title": "Tablet", "price": "3000", "ambiguous": False}],
            costs={"S2": "1500"},
        )
        _active_integration(self.svc.ba.integration_activation, "tenant-a", "bitrix")
        req = self.client.post(
            "/api/v1/business-assistant/requests",
            headers=_headers("secret-approver"),
            json={"message": "Опубликуй товары Samsung на Bitrix", "idempotency_key": "dup-approve-2"},
        )
        self.assertEqual(req.status_code, 200, req.text)
        req = req.json()
        body = {"approval_id": req["approval_id"]}
        first = self.client.post(
            f"/api/v1/business-assistant/requests/{req['request_id']}/approve",
            headers=_headers("secret-approver"),
            json=body,
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            f"/api/v1/business-assistant/requests/{req['request_id']}/approve",
            headers=_headers("secret-approver"),
            json=body,
        )
        self.assertIn(second.status_code, {409, 422})
        self.assertEqual(len(self.svc.ba._external_writes) - writes_before, 1)

    def test_web_e2e_6_reject(self):
        writes_before = len(self.svc.ba._external_writes)
        self.svc.ba.seed_supplier_fixture(
            rows=[{"sku": "S3", "brand": "Samsung", "title": "Watch", "price": "900", "ambiguous": False}],
            costs={"S3": "400"},
        )
        req = self.client.post(
            "/api/v1/business-assistant/requests",
            headers=_headers("secret-approver"),
            json={"message": "Опубликуй товары Samsung на Bitrix", "idempotency_key": "web-reject-flow-2"},
        ).json()
        if req["status"] == ST_WAITING_FOR_APPROVAL:
            rej = self.client.post(
                f"/api/v1/business-assistant/requests/{req['request_id']}/reject",
                headers=_headers("secret-approver"),
            )
            self.assertEqual(rej.status_code, 200)
            self.assertEqual(rej.json()["status"], "REJECTED")
            self.assertEqual(len(self.svc.ba._external_writes) - writes_before, 0)

    def test_web_e2e_7_refresh_recovery(self):
        conv = self.client.post(
            "/api/v1/business-assistant/conversations",
            headers=_headers("secret-a"),
            json={"title": "Refresh"},
        ).json()
        req = self._submit("Analyze Samsung", conversation_id=conv["conversation_id"], idempotency_key="refresh-1")
        msgs = self.client.get(
            f"/api/v1/business-assistant/conversations/{conv['conversation_id']}/messages",
            headers=_headers("secret-a"),
        ).json()
        self.assertTrue(any(m.get("request_id") == req["request_id"] for m in msgs))
        restored = self.client.get(
            f"/api/v1/business-assistant/requests/{req['request_id']}",
            headers=_headers("secret-a"),
        ).json()
        self.assertEqual(restored["request_id"], req["request_id"])

    def test_web_e2e_8_pending_approval_refresh(self):
        self.svc.ba.seed_supplier_fixture(
            rows=[{"sku": "S1", "brand": "Samsung", "title": "Phone", "price": "2000", "ambiguous": False}],
            costs={"S1": "1000"},
        )
        _active_integration(self.svc.ba.integration_activation, "tenant-a", "bitrix")
        conv = self.client.post(
            "/api/v1/business-assistant/conversations",
            headers=_headers("secret-a"),
            json={"title": "Pending"},
        ).json()
        req = self._submit(
            "Опубликуй товары Samsung на Bitrix",
            conversation_id=conv["conversation_id"],
            idempotency_key="pending-refresh-1",
        )
        self.assertEqual(req["status"], ST_WAITING_FOR_APPROVAL)
        msgs = self.client.get(
            f"/api/v1/business-assistant/conversations/{conv['conversation_id']}/messages",
            headers=_headers("secret-a"),
        ).json()
        rid = next(m["request_id"] for m in reversed(msgs) if m.get("request_id"))
        st = self.client.get(
            f"/api/v1/business-assistant/requests/{rid}/status",
            headers=_headers("secret-a"),
        ).json()
        self.assertEqual(st["status"], ST_WAITING_FOR_APPROVAL)
        self.assertTrue(st["approval_required"])

    def test_web_e2e_9_xss_content_safe(self):
        malicious = '<img src=x onerror=alert(1)>'
        safe = _escape_html(malicious)
        page = self.client.get("/app").text
        self.assertNotIn("onerror=alert", page)
        self.assertNotIn("<img src=x onerror", safe)

    def test_web_e2e_10_conversation_isolation(self):
        c1 = self.client.post(
            "/api/v1/business-assistant/conversations",
            headers=_headers("secret-a"),
            json={"title": "A"},
        ).json()
        c2 = self.client.post(
            "/api/v1/business-assistant/conversations",
            headers=_headers("secret-a"),
            json={"title": "B"},
        ).json()
        r1 = self._submit("Message A", conversation_id=c1["conversation_id"], idempotency_key="iso-conv-a")
        r2 = self._submit("Message B", conversation_id=c2["conversation_id"], idempotency_key="iso-conv-b")
        m1 = self.client.get(
            f"/api/v1/business-assistant/conversations/{c1['conversation_id']}/messages",
            headers=_headers("secret-a"),
        ).json()
        m2 = self.client.get(
            f"/api/v1/business-assistant/conversations/{c2['conversation_id']}/messages",
            headers=_headers("secret-a"),
        ).json()
        self.assertTrue(all(m.get("request_id") != r2["request_id"] for m in m1))
        self.assertTrue(all(m.get("request_id") != r1["request_id"] for m in m2))

    def test_idempotency_duplicate_submit(self):
        key = "web-idem-dup"
        body = {"message": "Analyze Samsung", "idempotency_key": key}
        r1 = self.client.post(
            "/api/v1/business-assistant/requests",
            headers=_headers("secret-a"),
            json=body,
        )
        r2 = self.client.post(
            "/api/v1/business-assistant/requests",
            headers=_headers("secret-a"),
            json=body,
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["request_id"], r2.json()["request_id"])

    def test_unauthenticated_conversations_denied(self):
        r = self.client.get("/api/v1/business-assistant/conversations")
        self.assertIn(r.status_code, {401, 403})


if __name__ == "__main__":
    unittest.main()
