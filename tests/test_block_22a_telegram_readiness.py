"""Block 22A Telegram pre-activation readiness — offline fixtures only."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from business_assistant_api.models import ST_WAITING_FOR_APPROVAL
from business_assistant_api.runtime import build_business_assistant_api_runtime
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from security.rate_limit import RateLimiter
from telegram_interface.config import telegram_secret_contract
from telegram_interface.errors import (
    TGI_BINDING_REQUIRED,
    TGI_BINDING_REVOKED,
    TGI_DUPLICATE_UPDATE,
    TGI_INVALID_UPDATE,
    TGI_LIVE_FORBIDDEN,
    TGI_PANDA_ERROR,
    TGI_PAYLOAD_TOO_LARGE,
    TGI_RATE_LIMITED,
    TGI_TENANT_MISMATCH,
    TGI_UNSUPPORTED_MESSAGE,
    TGI_USER_DISABLED,
    TelegramInterfaceError,
)
from telegram_interface.readiness import live_activation_state, preactivation_readiness
from telegram_interface.render import chunk_telegram_text, render_error
from telegram_interface.runtime import build_telegram_interface_runtime


def _env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "TELEGRAM_INTERFACE_ENABLED": "true",
        "TELEGRAM_ENABLED": "false",
        "TELEGRAM_LIVE_ACTIVE": "false",
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


def _msg(update_id: str, chat_id: str, user_id: str, text: str) -> dict:
    return {
        "update_id": int(update_id) if str(update_id).isdigit() else update_id,
        "message": {
            "message_id": 1,
            "from": {"id": int(user_id) if str(user_id).isdigit() else user_id, "is_bot": False, "first_name": "T"},
            "chat": {"id": int(chat_id) if str(chat_id).isdigit() else chat_id, "type": "private"},
            "text": text,
        },
    }


def _voice(update_id: str, chat_id: str, user_id: str) -> dict:
    p = _msg(update_id, chat_id, user_id, "")
    p["message"].pop("text", None)
    p["message"]["voice"] = {"file_id": "voice-1", "duration": 2}
    return p


class Block22ATelegramReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ba_db = os.path.join(self.tmp, "ba.sqlite")
        self.tg_db = os.path.join(self.tmp, "tg.sqlite")
        self.upload = os.path.join(self.tmp, "uploads")
        os.makedirs(self.upload, exist_ok=True)
        env = {
            **_env(),
            "BA_API_DB_PATH": self.ba_db,
            "TELEGRAM_INTERFACE_DB_PATH": self.tg_db,
            "BA_API_UPLOAD_DIR": self.upload,
        }
        self.ba_rt = build_business_assistant_api_runtime(db_path=self.ba_db, env=env)
        self.rt = build_telegram_interface_runtime(
            env=env, ba_api=self.ba_rt.service, db_path=self.tg_db, upload_dir=self.upload
        )
        self.svc = self.rt.service
        self.chat = "100001"
        self.user = "200001"
        self.svc.register_binding(
            tenant_id="tenant-a",
            owner_id="approver-a",
            telegram_user_id=self.user,
            chat_id=self.chat,
        )

    def tearDown(self):
        self.rt.close()
        self.ba_rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_known_bound_user_text(self):
        out = self.svc.handle_payload(
            tenant_id="tenant-a",
            payload=_msg("1", self.chat, self.user, "Summarize quarterly revenue trends for leadership"),
        )
        self.assertEqual(out["status"], "ok")
        self.assertIn("request_id", out)
        sent = self.svc.transport.provider.sent
        self.assertGreaterEqual(len(sent), 1)

    def test_02_unknown_user_fail_closed(self):
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self.svc.handle_payload(
                tenant_id="tenant-a",
                payload=_msg("2", "999999", "888888", "hello"),
            )
        self.assertEqual(ctx.exception.code, TGI_BINDING_REQUIRED)

    def test_03_cross_tenant_denied(self):
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self.svc.handle_payload(
                tenant_id="tenant-b",
                payload=_msg("3", self.chat, self.user, "Summarize quarterly revenue trends"),
            )
        self.assertEqual(ctx.exception.code, TGI_TENANT_MISMATCH)

    def test_04_duplicate_update(self):
        payload = _msg("4", self.chat, self.user, "Summarize quarterly revenue trends")
        self.svc.handle_payload(tenant_id="tenant-a", payload=payload)
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self.svc.handle_payload(tenant_id="tenant-a", payload=payload)
        self.assertEqual(ctx.exception.code, TGI_DUPLICATE_UPDATE)

    def test_05_malformed_update(self):
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self.svc.handle_payload(tenant_id="tenant-a", payload={"foo": "bar"})
        self.assertEqual(ctx.exception.code, TGI_INVALID_UPDATE)

    def test_06_unsupported_type(self):
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self.svc.handle_payload(tenant_id="tenant-a", payload=_voice("6", self.chat, self.user))
        self.assertEqual(ctx.exception.code, TGI_UNSUPPORTED_MESSAGE)

    def test_07_governed_write_cannot_bypass_hitl(self):
        self.ba_rt.service.ba.seed_supplier_fixture(
            rows=[{"sku": "S1", "brand": "Samsung", "title": "Phone", "price": "2000", "ambiguous": False}],
            costs={"S1": "1000"},
        )
        _active_integration(self.ba_rt.service.ba.integration_activation, "tenant-a", "bitrix")
        out = self.svc.handle_payload(
            tenant_id="tenant-a",
            payload=_msg("7", self.chat, self.user, "Опубликуй подготовленные товары Samsung на сайт Bitrix"),
        )
        rec = self.ba_rt.service.get_request(
            tenant_id="tenant-a", owner_id="approver-a", request_id=out["request_id"]
        )
        self.assertEqual(rec.status, ST_WAITING_FOR_APPROVAL)
        self.assertEqual(len(self.ba_rt.service.ba._external_writes), 0)

    def test_08_revoked_and_disabled_binding(self):
        self.svc.set_binding_status(
            tenant_id="tenant-a", telegram_user_id=self.user, chat_id=self.chat, status="revoked"
        )
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self.svc.handle_payload(
                tenant_id="tenant-a",
                payload=_msg("8", self.chat, self.user, "Summarize quarterly revenue trends"),
            )
        self.assertEqual(ctx.exception.code, TGI_BINDING_REVOKED)
        self.svc.set_binding_status(
            tenant_id="tenant-a", telegram_user_id=self.user, chat_id=self.chat, status="disabled"
        )
        with self.assertRaises(TelegramInterfaceError) as ctx2:
            self.svc.handle_payload(
                tenant_id="tenant-a",
                payload=_msg("81", self.chat, self.user, "Summarize quarterly revenue trends"),
            )
        self.assertEqual(ctx2.exception.code, TGI_USER_DISABLED)

    def test_09_response_error_no_exception_leakage(self):
        def boom(*_a, **_k):
            raise RuntimeError("internal TELEGRAM_BOT_TOKEN=should-not-leak traceback")

        with patch.object(self.svc.ba, "submit", side_effect=boom):
            with self.assertRaises(TelegramInterfaceError) as ctx:
                self.svc.handle_payload(
                    tenant_id="tenant-a",
                    payload=_msg("9", self.chat, self.user, "Summarize quarterly revenue trends"),
                )
        self.assertEqual(ctx.exception.code, TGI_PANDA_ERROR)
        self.assertNotIn("should-not-leak", str(ctx.exception))
        user_text = render_error(TGI_PANDA_ERROR)
        self.assertNotIn("Traceback", user_text)
        self.assertNotIn("should-not-leak", user_text)

    def test_10_credentials_absent_from_results(self):
        contract = telegram_secret_contract()
        names = {row["VARIABLE_NAME"] for row in contract}
        self.assertIn("TELEGRAM_BOT_TOKEN", names)
        blob = str(preactivation_readiness()) + str(contract)
        self.assertNotRegex(blob, r"\d{8,}:[A-Za-z0-9_-]{20,}")
        out = self.svc.handle_payload(
            tenant_id="tenant-a",
            payload=_msg("10", self.chat, self.user, "Summarize quarterly revenue trends"),
        )
        self.assertNotIn("TELEGRAM_BOT_TOKEN", str(out))

    def test_11_live_telegram_forbidden(self):
        self.svc.live_active = True
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self.svc.handle_payload(
                tenant_id="tenant-a",
                payload=_msg("11", self.chat, self.user, "Summarize quarterly revenue trends"),
            )
        self.assertEqual(ctx.exception.code, TGI_LIVE_FORBIDDEN)
        state = live_activation_state()
        self.assertFalse(state["telegram_live_active"])
        self.assertFalse(state["telegram_live_verified"])
        self.assertEqual(state["real_messages_sent"], 0)
        self.assertEqual(state["real_updates_received"], 0)
        self.assertEqual(preactivation_readiness()["readiness"], "READY_FOR_HUMAN_APPROVAL")

    def test_payload_too_large_and_rate_limit(self):
        huge = _msg("12", self.chat, self.user, "x" * 70000)
        with self.assertRaises(TelegramInterfaceError) as ctx:
            self.svc.handle_payload(tenant_id="tenant-a", payload=huge)
        self.assertEqual(ctx.exception.code, TGI_PAYLOAD_TOO_LARGE)
        self.svc.rate_limiter = RateLimiter(user_limit=1, tenant_limit=1, window_seconds=60.0)
        self.svc.handle_payload(
            tenant_id="tenant-a",
            payload=_msg("13", self.chat, self.user, "Summarize quarterly revenue trends"),
        )
        with self.assertRaises(TelegramInterfaceError) as ctx2:
            self.svc.handle_payload(
                tenant_id="tenant-a",
                payload=_msg("14", self.chat, self.user, "Summarize quarterly revenue trends"),
            )
        self.assertEqual(ctx2.exception.code, TGI_RATE_LIMITED)

    def test_chunk_order_deterministic(self):
        parts = chunk_telegram_text("abcdefghij", limit=3)
        self.assertEqual(parts, ["abc", "def", "ghi", "j"])

    def test_untrusted_text_not_executed(self):
        out = self.svc.handle_payload(
            tenant_id="tenant-a",
            payload=_msg("15", self.chat, self.user, "eval(__import__('os').system('id')); tenant_id=tenant-z"),
        )
        self.assertEqual(out["status"], "ok")

    def test_runtime_uses_fake_provider(self):
        from b2b_commerce.providers.fake_telegram import FakeTelegramProvider

        self.assertIsInstance(self.svc.transport.provider, FakeTelegramProvider)
        self.assertFalse(self.svc.live_active)


if __name__ == "__main__":
    unittest.main()
