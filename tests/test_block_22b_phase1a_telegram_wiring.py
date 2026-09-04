"""Block 22B Phase 1A — Telegram activation wiring (offline / synthetic only)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from b2b_commerce.providers.fake_telegram import FakeTelegramProvider
from business_assistant_api.runtime import build_business_assistant_api_runtime
from config.runtime_config import PROFILE_DEVELOPMENT, PROFILE_SINGLE_NODE, resolve_profile
from integrations.production.adapters.telegram import ProductionTelegramProvider
from production_foundation.config import validate_production_config
from security.api_auth import configure_security
from security.auth import AuthService
from telegram_interface.config import require_durable_telegram_db, telegram_live_network_selected
from telegram_interface.errors import TGI_LIVE_FORBIDDEN, TelegramInterfaceError
from telegram_interface.router import configure_telegram_interface_router
from telegram_interface.runtime import build_telegram_interface_runtime, select_telegram_interface_provider
from telegram_interface.transport import ProviderTelegramTransport


SYNTH_TOKEN = "synthetic-test-token-not-real"
SYNTH_SECRET = "synthetic-webhook-secret"


def _auth_env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-admin|tenant-a|admin-a|admin|secret-admin;"
            "key-user|tenant-a|user-a|user|secret-user;"
            "key-admin-b|tenant-b|admin-b|admin|secret-admin-b"
        ),
    }


def _ba_env(tmp: str) -> dict:
    return {
        **_auth_env(),
        "TELEGRAM_INTERFACE_ENABLED": "true",
        "TELEGRAM_ENABLED": "false",
        "TELEGRAM_LIVE_ACTIVE": "false",
        "TELEGRAM_WEBHOOK_SECRET": SYNTH_SECRET,
        "BA_API_DB_PATH": os.path.join(tmp, "ba.sqlite"),
        "TELEGRAM_INTERFACE_DB_PATH": os.path.join(tmp, "tg.sqlite"),
        "BA_API_UPLOAD_DIR": os.path.join(tmp, "uploads"),
    }


class FakeTelegramHttp:
    """Deterministic HTTP stub — no sockets."""

    def __init__(self):
        self.ops: list[str] = []

    def request(self, method, url, **_kwargs):
        op = str(url).rsplit("/", 1)[-1]
        self.ops.append(op)
        class _Resp:
            def json(self):
                if op == "getMe":
                    return {"ok": True, "result": {"id": 1, "is_bot": True, "username": "panda_fixture_bot"}}
                return {"ok": True, "result": {"message_id": 7}}

        return _Resp()

    def close(self):
        return None


class LiveConfigMatrixTests(unittest.TestCase):
    def test_a_interface_disabled_runtime_unavailable(self):
        with self.assertRaises(RuntimeError):
            build_telegram_interface_runtime(env={"TELEGRAM_INTERFACE_ENABLED": "false"})

    def test_a_disabled_router_openapi_and_503(self):
        app = FastAPI()
        app.include_router(configure_telegram_interface_router(None, webhook_secret=""))
        client = TestClient(app)
        spec = client.get("/openapi.json").json()
        self.assertIn("/api/v1/telegram/webhook/{tenant_id}", spec["paths"])
        r = client.post("/api/v1/telegram/webhook/tenant-a", json={"update_id": 1})
        self.assertEqual(r.status_code, 503)

    def test_b_fixture_offline_fake_provider(self):
        p = select_telegram_interface_provider({"TELEGRAM_LIVE_ACTIVE": "false", "TELEGRAM_ENABLED": "false"})
        self.assertIsInstance(p, FakeTelegramProvider)

    def test_c_production_live_not_approved_network_forbidden(self):
        self.assertFalse(
            telegram_live_network_selected(
                {"PANDA_ENV": "production", "TELEGRAM_ENABLED": "true", "TELEGRAM_LIVE_ACTIVE": "false"}
            )
        )
        p = select_telegram_interface_provider(
            {"PANDA_ENV": "production", "TELEGRAM_ENABLED": "true", "TELEGRAM_LIVE_ACTIVE": "false"}
        )
        self.assertIsInstance(p, FakeTelegramProvider)

    def test_d_explicit_live_selects_production_provider(self):
        env = {
            "TELEGRAM_LIVE_ACTIVE": "true",
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": SYNTH_TOKEN,
            "TELEGRAM_WEBHOOK_SECRET": SYNTH_SECRET,
        }
        p = select_telegram_interface_provider(env)
        self.assertIsInstance(p, ProductionTelegramProvider)
        self.assertNotIn(SYNTH_TOKEN, repr(p))
        self.assertIsNone(p._http)

    def test_e_explicit_live_missing_token_fail_closed(self):
        env = {"TELEGRAM_LIVE_ACTIVE": "true", "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": ""}
        with self.assertRaises(RuntimeError):
            select_telegram_interface_provider(env)

    def test_f_explicit_live_ephemeral_db_fail_closed(self):
        env = {
            "TELEGRAM_LIVE_ACTIVE": "true",
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": SYNTH_TOKEN,
        }
        with self.assertRaises(RuntimeError):
            require_durable_telegram_db(env, "./telegram_interface.sqlite")

    def test_g_explicit_live_never_silent_fake(self):
        env = {
            "TELEGRAM_LIVE_ACTIVE": "true",
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": SYNTH_TOKEN,
        }
        p = select_telegram_interface_provider(env)
        self.assertFalse(isinstance(p, FakeTelegramProvider))

    def test_runtime_profile_diagnosis(self):
        self.assertEqual(resolve_profile({}), PROFILE_DEVELOPMENT)
        self.assertEqual(resolve_profile({"PANDA_ENV": "production"}), PROFILE_SINGLE_NODE)

    def test_production_config_warn_is_operator_gap(self):
        report = validate_production_config(
            {
                "PANDA_ENV": "production",
                "PANDA_DATA_DIR": "/data",
                "PUBLIC_URL": "https://example.invalid",
                "SECURITY_AUTH_MODE": "required",
            }
        )
        self.assertIn(report.overall, {"WARN", "FAIL", "PASS"})


class LiveOutboundFakeHttpTests(unittest.TestCase):
    def test_outbound_uses_injected_http_not_network(self):
        provider = ProductionTelegramProvider(bot_token=SYNTH_TOKEN)
        http = FakeTelegramHttp()
        provider._http = http
        transport = ProviderTelegramTransport(provider=provider, tenant_id="tenant-a", live_network=True)
        from telegram_interface.transport import OutboundMessage

        transport.send(OutboundMessage(chat_id="1", text="hello", idempotency_key="k1"))
        self.assertEqual(http.ops, ["sendMessage"])
        self.assertNotIn(SYNTH_TOKEN, repr(provider))

    def test_production_provider_without_live_flag_is_forbidden(self):
        provider = ProductionTelegramProvider(bot_token=SYNTH_TOKEN)
        provider._http = FakeTelegramHttp()
        transport = ProviderTelegramTransport(provider=provider, tenant_id="tenant-a", live_network=False)
        from telegram_interface.transport import OutboundMessage

        with self.assertRaises(TelegramInterfaceError) as ctx:
            transport.send(OutboundMessage(chat_id="1", text="hello", idempotency_key="k2"))
        self.assertEqual(ctx.exception.code, TGI_LIVE_FORBIDDEN)
        self.assertEqual(provider._http.ops, [])


class BindingAdminAndWebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "uploads"), exist_ok=True)
        env = _ba_env(self.tmp)
        self.ba = build_business_assistant_api_runtime(db_path=env["BA_API_DB_PATH"], env=env)
        self.rt = build_telegram_interface_runtime(
            env=env, ba_api=self.ba.service, db_path=env["TELEGRAM_INTERFACE_DB_PATH"], upload_dir=env["BA_API_UPLOAD_DIR"]
        )
        configure_security(auth=AuthService(env=_auth_env()))
        app = FastAPI()
        app.include_router(configure_telegram_interface_router(self.rt.service, webhook_secret=SYNTH_SECRET))
        self.client = TestClient(app)

    def tearDown(self):
        self.rt.close()
        self.ba.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_admin_binding_e2e(self):
        body = {
            "tenant_id": "tenant-a",
            "owner_id": "owner-a",
            "telegram_user_id": "tg-1",
            "chat_id": "chat-1",
        }
        denied = self.client.post("/api/v1/telegram/admin/bindings", json=body, headers={"X-API-Key": "secret-user"})
        self.assertEqual(denied.status_code, 403)
        cross = self.client.post(
            "/api/v1/telegram/admin/bindings",
            json={**body, "tenant_id": "tenant-b"},
            headers={"X-API-Key": "secret-admin"},
        )
        self.assertEqual(cross.status_code, 403)
        created = self.client.post("/api/v1/telegram/admin/bindings", json=body, headers={"X-API-Key": "secret-admin"})
        self.assertEqual(created.status_code, 200)
        self.assertFalse(created.json()["idempotent"])
        replay = self.client.post("/api/v1/telegram/admin/bindings", json=body, headers={"X-API-Key": "secret-admin"})
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent"])
        readback = self.client.get(
            "/api/v1/telegram/admin/bindings",
            params={"tenant_id": "tenant-a", "telegram_user_id": "tg-1", "chat_id": "chat-1"},
            headers={"X-API-Key": "secret-admin"},
        )
        self.assertEqual(readback.status_code, 200)
        self.assertEqual(readback.json()["status"], "active")
        self.client.post(
            "/api/v1/telegram/admin/bindings/status",
            json={**body, "status": "revoked"},
            headers={"X-API-Key": "secret-admin"},
        )
        payload = {
            "update_id": 9001,
            "message": {
                "message_id": 1,
                "from": {"id": 1, "is_bot": False, "first_name": "T"},
                "chat": {"id": 1, "type": "private"},
                "text": "hi",
            },
        }
        payload["message"]["from"]["id"] = "tg-1"
        payload["message"]["chat"]["id"] = "chat-1"
        from telegram_interface.errors import TGI_BINDING_REVOKED, TGI_USER_DISABLED, TGI_BINDING_REQUIRED

        with self.assertRaises(Exception) as ctx:
            self.rt.service.handle_payload(
                tenant_id="tenant-a",
                payload={
                    "update_id": 9001,
                    "message": {
                        "from": {"id": "tg-1"},
                        "chat": {"id": "chat-1"},
                        "text": "hi",
                    },
                },
            )
        self.assertEqual(ctx.exception.code, TGI_BINDING_REVOKED)
        self.client.post(
            "/api/v1/telegram/admin/bindings/status",
            json={**body, "status": "disabled"},
            headers={"X-API-Key": "secret-admin"},
        )
        with self.assertRaises(Exception) as ctx2:
            self.rt.service.handle_payload(
                tenant_id="tenant-a",
                payload={
                    "update_id": 9002,
                    "message": {"from": {"id": "tg-1"}, "chat": {"id": "chat-1"}, "text": "hi"},
                },
            )
        self.assertEqual(ctx2.exception.code, TGI_USER_DISABLED)
        with self.assertRaises(Exception) as ctx3:
            self.rt.service.handle_payload(
                tenant_id="tenant-a",
                payload={
                    "update_id": 9003,
                    "message": {"from": {"id": "unknown"}, "chat": {"id": "nope"}, "text": "hi"},
                },
            )
        self.assertEqual(ctx3.exception.code, TGI_BINDING_REQUIRED)
        events = self.rt.store.list_binding_audit(tenant_id="tenant-a")
        self.assertTrue(events)
        blob = str(events)
        self.assertNotIn(SYNTH_TOKEN, blob)
        self.assertNotIn("first_name", blob)

    def test_fixture_path_still_fake(self):
        self.assertIsInstance(self.rt.service.transport.provider, FakeTelegramProvider)

    def test_durable_path_under_data_dir(self):
        env = {
            "TELEGRAM_LIVE_ACTIVE": "true",
            "TELEGRAM_ENABLED": "true",
            "PANDA_DATA_DIR": self.tmp,
            "TELEGRAM_BOT_TOKEN": SYNTH_TOKEN,
        }
        path = os.path.join(self.tmp, "telegram_interface.sqlite")
        require_durable_telegram_db(env, path)


class RuntimeLiveSelectionTests(unittest.TestCase):
    def test_live_runtime_with_data_dir_and_synthetic_token(self):
        tmp = tempfile.mkdtemp()
        try:
            env = {
                **_ba_env(tmp),
                "TELEGRAM_LIVE_ACTIVE": "true",
                "TELEGRAM_ENABLED": "true",
                "TELEGRAM_BOT_TOKEN": SYNTH_TOKEN,
                "TELEGRAM_WEBHOOK_SECRET": SYNTH_SECRET,
                "PANDA_DATA_DIR": tmp,
                "TELEGRAM_INTERFACE_DB_PATH": os.path.join(tmp, "telegram_interface.sqlite"),
            }
            os.makedirs(env["BA_API_UPLOAD_DIR"], exist_ok=True)
            ba = build_business_assistant_api_runtime(db_path=env["BA_API_DB_PATH"], env=env)
            rt = build_telegram_interface_runtime(
                env=env, ba_api=ba.service, db_path=env["TELEGRAM_INTERFACE_DB_PATH"], upload_dir=env["BA_API_UPLOAD_DIR"]
            )
            self.assertIsInstance(rt.service.transport.provider, ProductionTelegramProvider)
            self.assertTrue(rt.service.transport.live_network)
            self.assertIsNone(rt.service.transport.provider._http)
            rt.close()
            ba.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
