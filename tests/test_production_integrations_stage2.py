"""Stage-2 production integration closure tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from b2b_commerce.errors import B2B_TELEGRAM_DUPLICATE_UPDATE, B2BCommerceError
from b2b_commerce.providers.fake_telegram import FakeTelegramProvider
from b2b_commerce.sqlite_store import SqliteB2BStore
from b2b_commerce.telegram import TelegramSendRequest
from integrations.production.adapters.billing import StripeBillingProvider
from integrations.production.adapters.commerce import SandboxCommerceAdapter
from integrations.production.adapters.email import FakeTransactionalEmailProvider, TransactionalEmailMessage
from integrations.production.adapters.telegram import ProductionTelegramProvider, verify_telegram_webhook
from integrations.production.credentials import credential_inventory
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.factory import build_production_integrations
from integrations.production.retry import RetryPolicy, execute_with_retry, is_retryable
from saas_product.billing import BillingService
from saas_product.entitlements import EntitlementService
from saas_product.providers.fake_billing import FakeBillingProvider
from saas_product.sqlite_store import SqliteSaaSProductStore
from seo_marketing.providers.fake_search_console import FakeSearchConsoleProvider
from integrations.production.adapters.seo import ProductionSearchConsoleProvider


class Stage2BillingTests(unittest.TestCase):
    def test_checkout_webhook_idempotent(self):
        store = SqliteSaaSProductStore(":memory:")
        provider = FakeBillingProvider()
        billing = BillingService(store=store, provider=provider, entitlements=EntitlementService())
        checkout = billing.create_checkout(
            tenant_id="tenant-a",
            plan_id="pro",
            plan_version="2026-01",
            idempotency_key="idem-1",
        )
        event = provider.complete_checkout(checkout["checkout_id"])
        first = billing.process_webhook(event_id=event.event_id, signature=event.signature, payload_hash=event.payload_hash)
        second = billing.process_webhook(event_id=event.event_id, signature=event.signature, payload_hash=event.payload_hash)
        self.assertEqual(first["status"], "processed")
        self.assertEqual(second["status"], "already_processed")
        store.close()

    def test_forged_webhook_rejected(self):
        store = SqliteSaaSProductStore(":memory:")
        billing = BillingService(store=store, provider=FakeBillingProvider(), entitlements=EntitlementService())
        with self.assertRaises(Exception):
            billing.process_webhook(event_id="evt-x", signature="bad", payload_hash="bad")
        store.close()

    def test_stripe_signature_verification(self):
        provider = StripeBillingProvider(secret_key="sk_test_x", webhook_secret="whsec_test", mode="test")
        body = json.dumps({"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {"metadata": {"tenant_id": "t1"}}}}).encode()
        import time as _time

        ts = str(int(_time.time()))
        signed = hmac.new(b"whsec_test", f"{ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
        header = f"t={ts},v1={signed}"
        payload = provider.verify_stripe_signature(body, header)
        self.assertEqual(payload["id"], "evt_1")

    def test_stripe_invalid_signature(self):
        provider = StripeBillingProvider(secret_key="sk_test_x", webhook_secret="whsec_test", mode="test")
        with self.assertRaises(ProductionProviderError):
            provider.verify_stripe_signature(b"{}", "t=1,v1=bad")


class Stage2TelegramTests(unittest.TestCase):
    def test_webhook_secret_required(self):
        with self.assertRaises(ProductionProviderError):
            verify_telegram_webhook(secret_token="secret", header_token="wrong", raw_body=b"{}")

    def test_webhook_accepts_valid_secret(self):
        payload = verify_telegram_webhook(
            secret_token="secret",
            header_token="secret",
            raw_body=json.dumps({"update_id": 1, "message": {"chat": {"id": 9}, "text": "hi"}}).encode(),
        )
        self.assertEqual(payload["update_id"], 1)

    def test_telegram_replay_idempotent(self):
        from b2b_commerce.service import B2BCommerceService
        from b2b_commerce.capabilities import CAP_B2B_ASSISTANT_USE, CAP_TELEGRAM_READ

        store = SqliteB2BStore(":memory:")
        svc = B2BCommerceService(store, telegram_provider=FakeTelegramProvider())
        tenant = "tenant-a"
        acc = svc.register_telegram_account(tenant_id=tenant, bot_id="bot1", capabilities=(CAP_TELEGRAM_READ,))
        svc.bind_telegram_chat(
            tenant_id=tenant,
            account_binding_id=acc.binding_id,
            chat_id="chat1",
            customer_id="cust1",
            capabilities=(CAP_TELEGRAM_READ,),
        )
        update = {"update_id": "42", "bot_id": "bot1", "chat_id": "chat1", "text": "quote SKU-1 x2"}
        svc.process_telegram_update(tenant_id=tenant, raw_update=update, capabilities=(CAP_TELEGRAM_READ, CAP_B2B_ASSISTANT_USE))
        with self.assertRaises(B2BCommerceError) as ctx:
            svc.process_telegram_update(tenant_id=tenant, raw_update=update, capabilities=(CAP_TELEGRAM_READ, CAP_B2B_ASSISTANT_USE))
        self.assertEqual(ctx.exception.code, B2B_TELEGRAM_DUPLICATE_UPDATE)

    def test_telegram_outbound_idempotent(self):
        provider = FakeTelegramProvider()
        req = TelegramSendRequest(tenant_id="t1", chat_id="1", text="hello", idempotency_key="k1")
        provider.send_message(req)
        provider.send_message(req)
        self.assertEqual(len(provider.sent), 1)


class Stage2EmailTests(unittest.TestCase):
    def test_invitation_email_idempotent(self):
        email = FakeTransactionalEmailProvider()
        msg = TransactionalEmailMessage(
            recipient="user@example.com",
            event_type="tenant_invitation",
            template_data={},
            idempotency_key="inv-1",
            tenant_id="tenant-a",
        )
        first = email.send(msg)
        second = email.send(msg)
        self.assertEqual(first.provider_message_id, second.provider_message_id)
        self.assertEqual(len(email.sent), 1)

    def test_header_injection_rejected(self):
        email = FakeTransactionalEmailProvider()
        with self.assertRaises(ProductionProviderError):
            email.send(
                TransactionalEmailMessage(
                    recipient="bad\r\nBcc: evil@x.com",
                    event_type="tenant_invitation",
                    template_data={},
                    idempotency_key="inv-2",
                )
            )

    def test_transient_then_success(self):
        email = FakeTransactionalEmailProvider(fail_transient=True)
        msg = TransactionalEmailMessage(recipient="a@b.com", event_type="tenant_invitation", template_data={}, idempotency_key="inv-3")
        with self.assertRaises(ProductionProviderError):
            email.send(msg)
        result = email.send(msg)
        self.assertEqual(result.status, "accepted")


class Stage2SpeechTests(unittest.TestCase):
    def test_fake_stt_marker(self):
        from ui_chat.voice.stt import FakeSpeechToTextProvider

        stt = FakeSpeechToTextProvider()
        text = stt.transcribe(audio=b"PANDA_STT_TEST:hello", mime_type="audio/wav")
        self.assertEqual(text, "hello")

    def test_fake_tts(self):
        from ui_chat.voice.tts import FakeTextToSpeechProvider

        tts = FakeTextToSpeechProvider()
        audio = tts.synthesize(text="hello")
        self.assertTrue(audio.startswith(b"RIFF"))


class Stage2CommerceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sandbox_read_write_idempotent(self):
        adapter = SandboxCommerceAdapter(provider_id="bitrix")
        read = await adapter.read(operation="stock.read", params={"sku": "A1"})
        self.assertEqual(read["sku"], "A1")
        write1 = await adapter.write(operation="stock.update", params={"sku": "A1", "stock": 5}, idempotency_key="w1")
        write2 = await adapter.write(operation="stock.update", params={"sku": "A1", "stock": 5}, idempotency_key="w1")
        self.assertEqual(write1["receipt_id"], write2["receipt_id"])


class Stage2SeoTests(unittest.TestCase):
    def test_property_binding_denied(self):
        provider = ProductionSearchConsoleProvider(property_id="sc-domain:mine.com")
        with self.assertRaises(Exception):
            provider.get_query_performance(
                tenant_id="t1",
                property_id="sc-domain:foreign.com",
                date_start="2026-01-01",
                date_end="2026-01-07",
            )

    def test_provenance_metadata(self):
        provider = ProductionSearchConsoleProvider(property_id="sc-domain:mine.com")
        result = provider.get_query_performance(
            tenant_id="t1",
            property_id="sc-domain:mine.com",
            date_start="2026-01-01",
            date_end="2026-01-07",
        )
        self.assertIn("retrieved_at", result)
        self.assertEqual(result["provider"], "google_search_console")


class Stage2FactoryTests(unittest.TestCase):
    def test_default_bundle_registers_providers(self):
        bundle = build_production_integrations(env={"SAAS_BILLING_PROVIDER": "fake", "SPEECH_PROVIDER": "fake"})
        matrix = bundle.registry.list_metadata()
        ids = {m["provider_id"] for m in matrix}
        self.assertIn("fake", ids)
        self.assertIn("telegram", ids)
        self.assertIn("speech_stt", ids)
        self.assertIn("google_search_console", ids)
        self.assertIn("openai", ids)

    def test_credential_inventory_no_raw_values(self):
        inv = credential_inventory({"OPENAI_API_KEY": "sk-test"})
        for row in inv:
            self.assertNotIn("sk-test", str(row))


class Stage2RetryTests(unittest.TestCase):
    def test_retryable_categories(self):
        self.assertTrue(is_retryable(ProviderErrorCategory.RATE_LIMITED))
        self.assertFalse(is_retryable(ProviderErrorCategory.AUTHENTICATION_FAILED))

    def test_bounded_retry(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ProductionProviderError(ProviderErrorCategory.PROVIDER_UNAVAILABLE, retryable=True, provider_id="x")
            return "ok"

        result = execute_with_retry(flaky, policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01))
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["n"], 2)


class Stage2ProductionSafetyTests(unittest.TestCase):
    def test_fake_billing_forbidden_in_production(self):
        from saas_product.deployment import validate_production_config

        report = validate_production_config(env={"PANDA_ENV": "production", "SAAS_BILLING_ENABLED": "true", "SAAS_BILLING_PROVIDER": "fake"})
        billing = next(c for c in report.checks if c.name == "billing_provider")
        self.assertEqual(billing.status, "FAIL")

    def test_admin_matrix_has_no_secrets(self):
        bundle = build_production_integrations(env={"OPENAI_API_KEY": "sk-live-secret"})
        for row in bundle.registry.list_metadata():
            self.assertNotIn("sk-live-secret", json.dumps(row))


class Stage2MediaTests(unittest.TestCase):
    def test_fake_image_generation(self):
        from product_media.providers.fake import FakeImageGenerationProvider

        gen = FakeImageGenerationProvider()
        result = gen.generate(prompt="test product", width=128, height=128)
        self.assertTrue(result.data.startswith(b"\x89PNG"))
        self.assertEqual(result.provider_id, "fake-image-gen")


if __name__ == "__main__":
    unittest.main()
