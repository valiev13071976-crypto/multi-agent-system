"""External System Connectivity & Secrets Management tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autonomy.models import sanitize_metadata
from integrations.auth import ApiKeyAuthStrategy, BearerAuthStrategy, OAuth2AuthStrategy
from integrations.circuit_breaker import STATE_CLOSED, STATE_OPEN, CircuitBreaker
from integrations.contracts import (
    AUTH_BEARER,
    CircuitBreakerPolicy,
    IntegrationDescriptor,
    OAuthTokenBundle,
)
from integrations.errors import (
    CircuitOpenError,
    CredentialInvalidError,
    HostNotAllowedError,
    IdempotencyConflictError,
    ScopeInsufficientError,
    SecretBackendUnavailableError,
    WebhookReplayError,
    WebhookSignatureInvalidError,
)
from integrations.http_client import IntegrationHttpClient
from integrations.ledger import OperationLedger
from integrations.providers import BANK, PAYMENT, PROVIDER_CONTRACTS
from integrations.registry import IntegrationRegistry
from integrations.runtime import build_secrets_backend
from integrations.secrets.encrypted_store import EncryptedLocalSecretsBackend, ExternalSecretsBackend
from integrations.secrets.env_backend import FailClosedSecretsBackend, MemorySecretsBackend
from integrations.service import IntegrationService
from integrations.webhooks import WebhookProcessor
from security.encryption import EncryptionService
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets


def _enc() -> EncryptionService:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    return EncryptionService(key=base64.urlsafe_b64decode(key.encode("ascii")))


def _desc(**kwargs) -> IntegrationDescriptor:
    base = dict(
        integration_id="int-1",
        tenant_id="tenant-a",
        provider="moysklad",
        integration_type="inventory_erp",
        adapter_id="moysklad",
        enabled=True,
        auth_strategy=AUTH_BEARER,
        credential_ref="ms-token",
        read_capabilities=("moysklad.read",),
        write_capabilities=("moysklad.write",),
        base_url="https://api.moysklad.ru",
        allowed_hosts=("api.moysklad.ru",),
    )
    base.update(kwargs)
    return IntegrationDescriptor(**base)


class SecretsBackendTests(unittest.TestCase):
    def test_tenant_isolation_memory(self):
        be = MemorySecretsBackend()
        be.put_secret(tenant_id="tenant-a", secret_ref="k1", value="secret-a")
        be.put_secret(tenant_id="tenant-b", secret_ref="k1", value="secret-b")
        self.assertEqual(be.get_secret(tenant_id="tenant-a", secret_ref="k1"), "secret-a")
        self.assertEqual(be.get_secret(tenant_id="tenant-b", secret_ref="k1"), "secret-b")
        self.assertIsNone(be.get_secret(tenant_id="tenant-a", secret_ref="missing"))

    def test_secret_not_in_metadata_serialization(self):
        be = MemorySecretsBackend()
        be.put_secret(tenant_id="t", secret_ref="k", value="super-secret-value-xyz")
        meta = be.metadata(tenant_id="t", secret_ref="k")
        dumped = json.dumps(meta.__dict__, default=str)
        self.assertNotIn("super-secret-value-xyz", dumped)

    def test_missing_backend_fail_closed(self):
        be = FailClosedSecretsBackend()
        with self.assertRaises(SecretBackendUnavailableError):
            be.get_secret(tenant_id="t", secret_ref="k")

    def test_external_unavailable_fail_closed(self):
        be = ExternalSecretsBackend(provider="vault", available=False)
        with self.assertRaises(SecretBackendUnavailableError):
            be.get_secret(tenant_id="t", secret_ref="k")

    def test_revoked_rejected(self):
        be = MemorySecretsBackend()
        be.put_secret(tenant_id="t", secret_ref="k", value="v")
        be.set_rotation_state(tenant_id="t", secret_ref="k", state="revoked")
        with self.assertRaises(CredentialInvalidError):
            be.get_secret(tenant_id="t", secret_ref="k")

    def test_rotation_switches_version(self):
        be = MemorySecretsBackend()
        h1 = be.put_secret(tenant_id="t", secret_ref="k", value="v1")
        h2 = be.rotate_secret(tenant_id="t", secret_ref="k", new_value="v2")
        self.assertGreater(h2.version, h1.version)
        self.assertEqual(be.get_secret(tenant_id="t", secret_ref="k"), "v2")
        self.assertEqual(be.get_secret(tenant_id="t", secret_ref="k", version=h1.version), "v1")

    def test_encrypted_local_no_plaintext_in_db(self):
        enc = _enc()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "sec.sqlite3")
            be = EncryptedLocalSecretsBackend(encryption=enc, path=path)
            be.put_secret(tenant_id="t", secret_ref="k", value="plain-secret-abc")
            import sqlite3

            conn = sqlite3.connect(path)
            row = conn.execute("SELECT ciphertext FROM integration_secrets").fetchone()
            conn.close()
            self.assertNotIn(b"plain-secret-abc", row[0].encode("utf-8"))
            self.assertEqual(be.get_secret(tenant_id="t", secret_ref="k"), "plain-secret-abc")
            be.close()


class AuthStrategyTests(unittest.TestCase):
    def test_api_key_header(self):
        m = ApiKeyAuthStrategy().build_auth(secret="sk-test", settings={})
        self.assertIn("Authorization", m.headers)
        self.assertTrue(m.headers["Authorization"].endswith("sk-test"))

    def test_bearer(self):
        m = BearerAuthStrategy().build_auth(secret="tok")
        self.assertEqual(m.headers["Authorization"], "Bearer tok")

    def test_query_secret_requires_opt_in(self):
        with self.assertRaises(Exception):
            ApiKeyAuthStrategy().build_auth(
                secret="sk", settings={"api_key_placement": "query"}
            )

    def test_oauth_refresh(self):
        def refresh(rt, settings):
            return OAuthTokenBundle(
                access_token="new-access",
                refresh_token=rt,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

        strat = OAuth2AuthStrategy(refresh_fn=refresh)
        token = strat.ensure_access_token(
            cache_key="c1",
            access_token="old",
            refresh_token="rt",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        self.assertEqual(token, "new-access")


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.reg = IntegrationRegistry(path=":memory:")
        self.secrets = MemorySecretsBackend()
        self.svc = IntegrationService(registry=self.reg, secrets_backend=self.secrets)

    def test_read_only_cannot_write(self):
        d = _desc(write_capabilities=())
        self.svc.register_integration(d)
        with self.assertRaises(ScopeInsufficientError):
            self.svc.assert_capability(d, capability="moysklad.write", is_write=True)

    def test_bank_write_denied(self):
        d = _desc(
            provider="bank",
            adapter_id="bank",
            write_capabilities=("bank.transfer",),
            read_capabilities=("bank.read",),
        )
        self.svc.register_integration(d)
        with self.assertRaises(ScopeInsufficientError):
            self.svc.assert_capability(d, capability="bank.transfer", is_write=True)

    def test_tenant_isolation_registry(self):
        self.svc.register_integration(_desc())
        self.assertIsNone(self.reg.get("tenant-b", "int-1"))


class HttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_arbitrary_host_denied(self):
        client = IntegrationHttpClient(secrets_backend=MemorySecretsBackend())
        d = _desc()
        from integrations.contracts import IntegrationOperationContext

        ctx = IntegrationOperationContext(
            tenant_id="tenant-a",
            integration_id="int-1",
            operation="get",
            request_id="r1",
        )
        with self.assertRaises(HostNotAllowedError):
            await client.request(
                d, ctx, method="GET", path="https://evil.example/x", secret="tok"
            )


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.secrets = MemorySecretsBackend()
        self.secrets.put_secret(tenant_id="tenant-a", secret_ref="whsec", value="wh-secret")
        self.proc = WebhookProcessor(secrets_backend=self.secrets, path=":memory:")

    def test_valid_signature(self):
        body = b'{"ok":true}'
        sig = hmac.new(b"wh-secret", body, hashlib.sha256).hexdigest()
        env = self.proc.process(
            tenant_id="tenant-a",
            integration_id="int-1",
            provider="payment",
            event_id="evt-1",
            event_type="payment.updated",
            body=body,
            signature_header=f"sha256={sig}",
            secret_ref="whsec",
        )
        self.assertTrue(env.signature_verified)

    def test_invalid_signature(self):
        with self.assertRaises(WebhookSignatureInvalidError):
            self.proc.process(
                tenant_id="tenant-a",
                integration_id="int-1",
                provider="payment",
                event_id="evt-2",
                event_type="payment.updated",
                body=b"{}",
                signature_header="sha256=deadbeef",
                secret_ref="whsec",
            )

    def test_replay_rejected(self):
        body = b"{}"
        sig = hmac.new(b"wh-secret", body, hashlib.sha256).hexdigest()
        kwargs = dict(
            tenant_id="tenant-a",
            integration_id="int-1",
            provider="payment",
            event_id="evt-dup",
            event_type="x",
            body=body,
            signature_header=f"sha256={sig}",
            secret_ref="whsec",
        )
        self.proc.process(**kwargs)
        with self.assertRaises(WebhookReplayError):
            self.proc.process(**kwargs)

    def test_ip_alone_cannot_authenticate(self):
        with self.assertRaises(WebhookSignatureInvalidError):
            self.proc.process(
                tenant_id="tenant-a",
                integration_id="int-1",
                provider="payment",
                event_id="evt-ip",
                event_type="x",
                body=b"{}",
                signature_header="",
                secret_ref="",
                source_ip="1.2.3.4",
                ip_allowlist=("1.2.3.4",),
                require_signature=False,
                allow_ip_only_auth=True,
            )


class CircuitBreakerTests(unittest.TestCase):
    def test_opens_on_transient_and_not_on_validation(self):
        cb = CircuitBreaker(CircuitBreakerPolicy(failure_threshold=2, cooldown_seconds=0.01))
        cb.record_failure("t", "i", error_code="tool_argument_invalid")
        cb.record_failure("t", "i", error_code="tool_argument_invalid")
        self.assertEqual(cb.get_state("t", "i"), STATE_CLOSED)
        cb.record_failure("t", "i", error_code="external_transient_failure")
        cb.record_failure("t", "i", error_code="external_transient_failure")
        self.assertEqual(cb.get_state("t", "i"), STATE_OPEN)
        with self.assertRaises(CircuitOpenError):
            cb.assert_allow("t", "i")
        cb.record_success("t", "i")
        self.assertEqual(cb.get_state("t", "i"), STATE_CLOSED)


class LedgerIdempotencyTests(unittest.TestCase):
    def test_duplicate_write_same_operation(self):
        led = OperationLedger(path=":memory:")
        a = led.begin(
            tenant_id="t",
            integration_id="i",
            operation_type="create",
            idempotency_key="idem-1",
            request_fingerprint="fp1",
        )
        led.complete(a["operation_id"], tenant_id="t", status="completed", result={"ok": True})
        b = led.begin(
            tenant_id="t",
            integration_id="i",
            operation_type="create",
            idempotency_key="idem-1",
            request_fingerprint="fp1",
        )
        self.assertEqual(a["operation_id"], b["operation_id"])
        with self.assertRaises(IdempotencyConflictError):
            led.begin(
                tenant_id="t",
                integration_id="i",
                operation_type="create",
                idempotency_key="idem-1",
                request_fingerprint="fp-other",
            )


class ProviderFoundationTests(unittest.TestCase):
    def test_contracts_present(self):
        for pid in (
            "moysklad",
            "onec",
            "erp_wms",
            "bitrix",
            "edo",
            "fiscal",
            "bank",
            "payment_gateway",
        ):
            self.assertIn(pid, PROVIDER_CONTRACTS)
        self.assertTrue(BANK.write_default_deny)
        self.assertTrue(PAYMENT.write_default_deny)


class HealthTests(unittest.TestCase):
    def test_health_states(self):
        reg = IntegrationRegistry(path=":memory:")
        secrets = MemorySecretsBackend()
        svc = IntegrationService(registry=reg, secrets_backend=secrets)
        svc.register_integration(_desc())
        secrets.put_secret(tenant_id="tenant-a", secret_ref="ms-token", value="tok")
        h = svc.check_health("tenant-a", "int-1")
        self.assertEqual(h.status, "healthy")
        secrets.set_rotation_state(tenant_id="tenant-a", secret_ref="ms-token", state="revoked")
        h2 = svc.check_health("tenant-a", "int-1")
        self.assertEqual(h2.status, "auth_failed")


class RedactionInvariantTests(unittest.TestCase):
    def test_sanitize_strips_secrets(self):
        cleaned = sanitize_metadata(
            {"api_key": "sk-live-123", "token": "abc", "safe": "ok", "note": "Bearer xyz"}
        )
        self.assertNotIn("api_key", cleaned)
        self.assertNotIn("token", cleaned)
        self.assertEqual(cleaned["safe"], "ok")
        self.assertIn("[REDACTED]", cleaned["note"])

    def test_audit_and_ledger_no_secret(self):
        reg = IntegrationRegistry(path=":memory:")
        secrets = MemorySecretsBackend()
        svc = IntegrationService(registry=reg, secrets_backend=secrets)
        svc.register_integration(_desc())
        secrets.put_secret(tenant_id="tenant-a", secret_ref="ms-token", value="TOPSECRETVALUE")
        events = reg.audit_events("tenant-a")
        blob = json.dumps(events)
        self.assertNotIn("TOPSECRETVALUE", blob)
        led = svc.ledger
        op = led.begin(
            tenant_id="tenant-a",
            integration_id="int-1",
            operation_type="x",
            idempotency_key="k",
        )
        led.complete(
            op["operation_id"],
            tenant_id="tenant-a",
            result={"authorization": "Bearer TOPSECRETVALUE", "ok": True},
        )
        stored = led.get(op["operation_id"], tenant_id="tenant-a")
        self.assertNotIn("TOPSECRETVALUE", stored["result_json"])


class ProductionWiringTests(unittest.TestCase):
    def test_compose_includes_integration_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "int.sqlite3")
            key = os.urandom(32)
            enc = EncryptionService(key=key, key_id="v1")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                encryption=enc,
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                    "INTEGRATION_ENABLED": "true",
                    "INTEGRATION_SECRETS_BACKEND": "encrypted_local",
                    "INTEGRATION_USE_SHARED_DB": "true",
                },
            )
            try:
                self.assertIsNotNone(runtime.integration_runtime)
                self.assertIsNotNone(runtime.tool_gateway)
                self.assertIs(
                    runtime.tool_gateway._integration_credential_store,  # noqa: SLF001
                    runtime.integration_runtime.credential_store,
                )
                health = runtime.integration_runtime.health()
                self.assertIn(health["integration_status"], {"healthy", "blocked", "degraded"})
                self.assertEqual(
                    health["secrets"].get("backend"), "encrypted_local"
                )
                # same ToolGateway object identity
                gw = runtime.tool_gateway
                self.assertIs(runtime.tool_gateway, gw)
            finally:
                runtime.close()

    def test_production_external_unavailable_fail_closed(self):
        be = build_secrets_backend(
            env={
                "INTEGRATION_SECRETS_BACKEND": "external",
                "INTEGRATION_SECRETS_MODE": "production",
                "INTEGRATION_EXTERNAL_SECRETS_AVAILABLE": "false",
            }
        )
        self.assertIsInstance(be, FailClosedSecretsBackend)


if __name__ == "__main__":
    unittest.main()
