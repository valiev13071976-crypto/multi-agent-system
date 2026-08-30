"""Block 16 — External Product / SaaS closure tests."""

from __future__ import annotations

import importlib
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi.testclient import TestClient

from saas_product.deployment import validate_production_config
from saas_product.plans import PLAN_STARTER
from saas_product.providers.fake_billing import FakeBillingProvider
from saas_product.runtime import build_saas_product_runtime
from saas_product.service import SaaSProductService
from security.api_auth import configure_security
from security.auth import AuthService
from security.identity import RequestSecurityContext


def _auth_env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-owner|tenant-a|owner-a|user|secret-owner;"
            "key-member|tenant-a|member-a|user|secret-member;"
            "key-b|tenant-b|user-b|user|secret-b;"
            "key-admin|tenant-a|admin-a|admin|secret-admin"
        ),
    }


def _headers(key: str, tenant: str | None = None) -> dict:
    h = {"X-API-Key": key}
    if tenant:
        h["X-Active-Tenant"] = tenant
    return h


class SaaSServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "saas.sqlite")
        os.environ["SAAS_PRODUCT_DB_PATH"] = self.db_path
        self.rt = build_saas_product_runtime(env={**os.environ, "SAAS_PRODUCT_DB_PATH": self.db_path})
        self.svc = self.rt.service
        self.owner = RequestSecurityContext(user_id="owner-a", tenant_id="tenant-a", roles=("user",), request_id="r1")

    def tearDown(self):
        if getattr(self, "rt", None) is not None:
            self.rt.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _setup_tenant(self):
        t = self.svc.create_tenant(self.owner, name="Acme")
        ctx = RequestSecurityContext(user_id="owner-a", tenant_id=t.tenant_id, roles=("user",), request_id="r1")
        return t, ctx

    def test_create_tenant_and_owner_membership(self):
        t, ctx = self._setup_tenant()
        mem = self.svc.store.get_active_membership("owner-a", t.tenant_id)
        self.assertIsNotNone(mem)
        self.assertEqual(mem.role, "OWNER")

    def test_invite_and_accept(self):
        t, ctx = self._setup_tenant()
        inv, token = self.svc.invite_member(ctx, email="member@example.com", role="MEMBER")
        member_ctx = RequestSecurityContext(user_id="member-a", tenant_id=t.tenant_id, roles=("user",), request_id="r2")
        mem = self.svc.accept_invitation(member_ctx, token=token)
        self.assertEqual(mem.tenant_id, t.tenant_id)

    def test_invite_replay_denied(self):
        t, ctx = self._setup_tenant()
        _, token = self.svc.invite_member(ctx, email="x@example.com")
        member_ctx = RequestSecurityContext(user_id="member-a", tenant_id=t.tenant_id, roles=("user",), request_id="r2")
        self.svc.accept_invitation(member_ctx, token=token)
        with self.assertRaises(Exception):
            self.svc.accept_invitation(member_ctx, token=token)

    def test_self_escalation_denied(self):
        t, ctx = self._setup_tenant()
        inv, token = self.svc.invite_member(ctx, email="m@example.com")
        member_ctx = RequestSecurityContext(user_id="member-a", tenant_id=t.tenant_id, roles=("user",), request_id="r2")
        mem = self.svc.accept_invitation(member_ctx, token=token)
        with self.assertRaises(Exception):
            self.svc.change_role(member_ctx, mem.membership_id, role="OWNER", expected_version=mem.version)

    def test_last_owner_protected(self):
        t, ctx = self._setup_tenant()
        mem = self.svc.store.get_active_membership("owner-a", t.tenant_id)
        with self.assertRaises(Exception):
            self.svc.remove_member(ctx, mem.membership_id, expected_version=mem.version)

    def test_billing_checkout_and_webhook(self):
        t, ctx = self._setup_tenant()
        checkout = self.svc.create_checkout(ctx, plan_id=PLAN_STARTER, plan_version="2026-01", idempotency_key="checkout-key-12345678")
        event = self.svc.billing.provider.complete_checkout(checkout["checkout_id"])
        result = self.svc.billing.process_webhook(event_id=event.event_id, signature=event.signature, payload_hash=event.payload_hash)
        self.assertEqual(result["status"], "processed")
        self.svc.billing.process_webhook(event_id=event.event_id, signature=event.signature, payload_hash=event.payload_hash)

    def test_unverified_webhook_denied(self):
        with self.assertRaises(Exception):
            self.svc.billing.process_webhook(event_id="evt-fake", signature="bad", payload_hash="bad")

    def test_quota_enforcement(self):
        t, ctx = self._setup_tenant()
        for i in range(100):
            self.svc.metering.record_request(tenant_id=t.tenant_id, user_id="owner-a", idempotency_key=f"req-{i}")
        with self.assertRaises(Exception):
            self.svc.enforce_entitlement(ctx, feature="chat", meter="requests_per_month", idempotency_key="req-over")

    def test_privacy_export(self):
        t, ctx = self._setup_tenant()
        job = self.svc.request_export(ctx)
        self.assertEqual(job["status"], "COMPLETED")

    def test_account_deletion_confirmation(self):
        t, ctx = self._setup_tenant()
        inv, token = self.svc.invite_member(ctx, email="del@example.com")
        member_ctx = RequestSecurityContext(user_id="member-a", tenant_id=t.tenant_id, roles=("user",), request_id="r2")
        self.svc.accept_invitation(member_ctx, token=token)
        req = self.svc.request_account_deletion(member_ctx)
        done = self.svc.confirm_deletion(member_ctx, req["job_id"], confirmation_token=req["confirmation_token"])
        self.assertEqual(done["status"], "COMPLETED")
        self.assertIsNone(self.svc.store.get_active_membership("member-a", t.tenant_id))
        user = self.svc.store.get_user("member-a")
        self.assertEqual(user.status, "DELETED")

    def test_concurrent_quota_race(self):
        t, ctx = self._setup_tenant()
        for i in range(99):
            self.svc.metering.record_request(tenant_id=t.tenant_id, user_id="owner-a", idempotency_key=f"race-{i}")

        def attempt(key: str):
            try:
                self.svc.enforce_entitlement(ctx, feature="chat", meter="requests_per_month", idempotency_key=key)
                return True
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, [f"race-final-{i}" for i in range(8)]))
        self.assertEqual(sum(results), 1)

    def test_backup_restore_state(self):
        t, ctx = self._setup_tenant()
        self.svc.invite_member(ctx, email="restore@example.com")
        tenant_id = t.tenant_id
        self.rt.close()
        backup_path = self.db_path + ".bak"
        shutil.copy2(self.db_path, backup_path)
        rt2 = build_saas_product_runtime(env={"SAAS_PRODUCT_DB_PATH": backup_path})
        restored = rt2.service.store.get_tenant(tenant_id)
        self.assertIsNotNone(restored)
        invs, total = rt2.service.store.list_invitations(tenant_id)
        self.assertGreaterEqual(total, 1)
        rt2.close()
        os.unlink(backup_path)
        self.rt = build_saas_product_runtime(env={"SAAS_PRODUCT_DB_PATH": self.db_path})

    def test_tenant_deletion_request(self):
        t, ctx = self._setup_tenant()
        req = self.svc.request_tenant_deletion(ctx)
        self.assertIn("confirmation_token", req)
        done = self.svc.confirm_deletion(ctx, req["job_id"], confirmation_token=req["confirmation_token"])
        self.assertEqual(done["status"], "COMPLETED")
        tenant = self.svc.store.get_tenant(t.tenant_id)
        self.assertEqual(tenant.status, "DELETED")

    def test_removed_member_denied(self):
        t, ctx = self._setup_tenant()
        inv, token = self.svc.invite_member(ctx, email="gone@example.com")
        member_ctx = RequestSecurityContext(user_id="member-a", tenant_id=t.tenant_id, roles=("user",), request_id="r2")
        mem = self.svc.accept_invitation(member_ctx, token=token)
        self.svc.remove_member(ctx, mem.membership_id, expected_version=mem.version)
        with self.assertRaises(Exception):
            self.svc.list_members(member_ctx)


class SaaSHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(_auth_env())
        import main as main_mod

        configure_security(auth=AuthService(env=_auth_env()))
        importlib.reload(main_mod)
        cls.client = TestClient(main_mod.app)
        cls.main = main_mod

    def test_product_page(self):
        r = self.client.get("/product")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Panda Account", r.text)

    def test_onboarding_requires_auth(self):
        r = self.client.get("/api/product/onboarding")
        self.assertIn(r.status_code, {401, 403})

    def test_tenant_lifecycle_http(self):
        r = self.client.post("/api/product/tenants", headers=_headers("secret-owner"), json={"name": "Test Co"})
        self.assertEqual(r.status_code, 200)
        tid = r.json()["tenant_id"]
        r2 = self.client.get("/api/product/members", headers=_headers("secret-owner", tid))
        self.assertEqual(r2.status_code, 200)
        self.assertGreaterEqual(r2.json()["page"]["total"], 1)

    def test_tenant_owner_not_platform_admin(self):
        r = self.client.get("/api/admin/ops/dashboard", headers=_headers("secret-owner"))
        self.assertEqual(r.status_code, 403)

    def test_cross_tenant_idor(self):
        r = self.client.post("/api/product/tenants", headers=_headers("secret-owner"), json={"name": "A"})
        tid_a = r.json()["tenant_id"]
        r = self.client.post("/api/product/tenants", headers=_headers("secret-b"), json={"name": "B"})
        tid_b = r.json()["tenant_id"]
        r = self.client.get("/api/product/members", headers=_headers("secret-b", tid_a))
        self.assertEqual(r.status_code, 403)

    def test_billing_webhook_http(self):
        r = self.client.post("/api/product/tenants", headers=_headers("secret-owner"), json={"name": "BillCo"})
        tid = r.json()["tenant_id"]
        r = self.client.post(
            "/api/product/billing/checkout",
            headers=_headers("secret-owner", tid),
            json={"plan_id": "starter", "plan_version": "2026-01", "idempotency_key": "idem-12345678"},
        )
        checkout_id = r.json()["checkout_id"]
        provider = self.main.saas_runtime.service.billing.provider
        event = provider.complete_checkout(checkout_id)
        r = self.client.post("/api/product/billing/webhook", json={"event_id": event.event_id, "signature": event.signature, "payload_hash": event.payload_hash})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/product/billing/status", headers=_headers("secret-owner", tid))
        self.assertEqual(r.json()["subscription"]["status"], "ACTIVE")

    def test_commercial_readiness(self):
        r = self.client.get("/api/product/readiness")
        self.assertEqual(r.status_code, 200)
        self.assertIn("overall", r.json())

    def test_production_fake_billing_fail(self):
        report = validate_production_config(env={"PANDA_ENV": "production", "SAAS_BILLING_ENABLED": "true", "SAAS_BILLING_PROVIDER": "fake", "SECURITY_AUTH_MODE": "required", "PANDA_API_KEYS": "k|t|u|user|s", "SAAS_PRODUCT_DB_PATH": "/tmp/x.sqlite"})
        self.assertEqual(report.overall, "FAIL")

    def test_chat_has_account_link(self):
        r = self.client.get("/")
        self.assertIn("/product", r.text)


class SaaSSecurityTests(unittest.TestCase):
    def test_invitation_token_hash_not_plaintext_in_store(self):
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, "saas.sqlite")
        rt = build_saas_product_runtime(env={"SAAS_PRODUCT_DB_PATH": db_path})
        ctx = RequestSecurityContext(user_id="u1", tenant_id="t1", roles=("user",), request_id="r")
        t = rt.service.create_tenant(ctx, name="X")
        actx = RequestSecurityContext(user_id="u1", tenant_id=t.tenant_id, roles=("user",), request_id="r")
        inv, token = rt.service.invite_member(actx, email="a@b.com")
        self.assertNotEqual(inv.token_hash, token)
        rt.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
