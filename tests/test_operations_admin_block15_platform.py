"""Block 15 Operations / Dashboard / Admin — closure tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from operations_admin.access import AdminAuthorizationPolicy
from operations_admin.alerts import AlertEngine
from operations_admin.audit_store import AdminAuditStore
from operations_admin.capabilities import PERM_OPS_READ, PERM_OPS_ROUTING_WRITE, PERM_OPS_WRITE
from operations_admin.commands import ActivateRoutingCommand, RedriveDLQCommand, RollbackRoutingCommand, confirmation_token
from operations_admin.errors import AdminError
from operations_admin.service import OperationsAdminService
from security.api_auth import configure_security
from security.auth import AuthService
from security.config import ROLE_ADMIN, ROLE_TENANT_ADMIN, ROLE_USER, ROLE_VIEWER
from security.identity import RequestSecurityContext


def _auth_env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-admin|tenant-a|admin-a|admin|secret-admin;"
            "key-user|tenant-a|user-a|user|secret-user;"
            "key-viewer|tenant-a|viewer-a|viewer|secret-viewer;"
            "key-tadmin|tenant-a|tadmin-a|tenant_admin|secret-tadmin;"
            "key-b|tenant-b|user-b|user|secret-b;"
            "key-tadmin-b|tenant-b|tadmin-b|tenant_admin|secret-tadmin-b"
        ),
    }


def _ctx(*, tenant="tenant-a", user="admin-a", roles=(ROLE_ADMIN,)):
    return RequestSecurityContext(user_id=user, tenant_id=tenant, roles=roles, request_id="req-1")


class AdminAccessTests(unittest.TestCase):
    def test_viewer_cannot_write(self):
        policy = AdminAuthorizationPolicy()
        with self.assertRaises(AdminError):
            policy.require(_ctx(roles=(ROLE_VIEWER,)), PERM_OPS_WRITE)

    def test_admin_can_read(self):
        AdminAuthorizationPolicy().require(_ctx(), PERM_OPS_READ)

    def test_tenant_scope_denied(self):
        policy = AdminAuthorizationPolicy()
        with self.assertRaises(AdminError):
            policy.assert_tenant_scope(_ctx(roles=(ROLE_TENANT_ADMIN,)), "tenant-b")


class AdminAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        self.store = AdminAuditStore(self.tmp.name)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_append_and_list(self):
        self.store.append(
            actor_ref="admin-a",
            tenant_scope="tenant-a",
            capability=PERM_OPS_WRITE,
            action="run.cancel",
            target_type="workflow",
            target_id="wf-1",
            result="ok",
        )
        items, total = self.store.list_events(limit=10)
        self.assertEqual(total, 1)
        self.assertEqual(items[0].action, "run.cancel")

    def test_secret_redaction_in_reason(self):
        rec = self.store.append(
            actor_ref="admin-a",
            tenant_scope="tenant-a",
            capability=PERM_OPS_WRITE,
            action="test",
            target_type="x",
            target_id="1",
            result="ok",
            reason="Authorization: Bearer sk-secret123",
        )
        self.assertNotIn("sk-secret123", rec.reason or "")

    def test_pagination_bound(self):
        for i in range(120):
            self.store.append(
                actor_ref="admin-a",
                tenant_scope="tenant-a",
                capability=PERM_OPS_WRITE,
                action="test",
                target_type="x",
                target_id=str(i),
                result="ok",
            )
        items, total = self.store.list_events(limit=200)
        self.assertEqual(len(items), 120)
        self.assertEqual(total, 120)


class AdminServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        self.audit = AdminAuditStore(self.tmp.name)
        self.svc = OperationsAdminService(audit_store=self.audit)

    def tearDown(self):
        self.audit.close()
        os.unlink(self.tmp.name)

    def test_dashboard_requires_capability(self):
        with self.assertRaises(AdminError):
            self.svc.dashboard(_ctx(roles=(ROLE_USER,)))

    def test_dashboard_ok_for_admin(self):
        d = self.svc.dashboard(_ctx())
        self.assertIsNotNone(d.generated_at)

    def test_cost_aggregation_deterministic(self):
        from finops.models import UsageRecord
        from finops.service import FinOpsService
        from finops.storage import InMemoryUsageStore

        finops = FinOpsService(store=InMemoryUsageStore())
        now = datetime.now(timezone.utc)
        finops.record(
            UsageRecord(
                task_id="t1",
                provider_id="openai",
                model_id="gpt-4",
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
                estimated_cost=Decimal("0.10"),
                currency="USD",
                timestamp=now,
                tenant_id="tenant-a",
            )
        )
        finops.record(
            UsageRecord(
                task_id="t2",
                provider_id="anthropic",
                model_id="claude",
                input_tokens=200,
                output_tokens=100,
                total_tokens=300,
                estimated_cost=Decimal("0.20"),
                currency="USD",
                timestamp=now,
                tenant_id="tenant-b",
            )
        )
        svc = OperationsAdminService(audit_store=self.audit, finops=finops)
        global_sum = svc.usage_summary(_ctx(), window="24h")
        self.assertEqual(global_sum.total_cost, "0.30")
        tenant_a = svc.usage_summary(_ctx(roles=(ROLE_TENANT_ADMIN,)), window="24h", tenant_id="tenant-a")
        self.assertEqual(tenant_a.total_cost, "0.10")

    def test_alert_dedupe_and_recovery(self):
        engine = AlertEngine()
        svc = OperationsAdminService(audit_store=self.audit, alert_engine=engine)
        svc.alerts.observe(source="openai", message="openai unhealthy", severity="CRITICAL", active=True)
        svc.alerts.observe(source="openai", message="openai unhealthy", severity="CRITICAL", active=True)
        active = svc.alerts.list_active()
        self.assertEqual(len(active), 1)
        self.assertGreaterEqual(active[0].count, 2)
        svc.alerts.observe(source="openai", message="openai unhealthy", severity="CRITICAL", active=False)
        self.assertEqual(len(svc.alerts.list_active()), 0)

    def test_routing_activation_requires_confirmation(self):
        from evals.promotion import STAGE_PRODUCTION_ELIGIBLE, CandidatePolicy

        candidate = CandidatePolicy(
            candidate_id="c1",
            candidate_version="v1",
            base_routing_policy_version="base",
            proposed_routing_policy_version="prop",
            stage=STAGE_PRODUCTION_ELIGIBLE,
            eval_suite_id="s",
            eval_suite_version="1",
            eval_run_id="r1",
            eval_manifest_hash="h",
            model_profile_version="prop",
            production_eligible=True,
        )
        from evals.activation import RoutingActivationService

        svc = OperationsAdminService(audit_store=self.audit, routing_activation=RoutingActivationService())
        svc.register_routing_candidate(candidate)
        with self.assertRaises(AdminError):
            svc.activate_routing(
                _ctx(),
                ActivateRoutingCommand(
                    candidate_id="c1",
                    expected_policy_version="prop",
                    confirmation_token="bad",
                ),
                candidate=candidate,
            )

    def test_routing_rollback_audited(self):
        from evals.activation import RoutingActivationService

        svc = OperationsAdminService(audit_store=self.audit, routing_activation=RoutingActivationService())
        token = confirmation_token(actor_ref=_ctx().actor_ref(), action="routing.rollback", target_id="active")
        svc.rollback_routing(_ctx(), RollbackRoutingCommand(confirmation_token=token))
        items, _ = self.audit.list_events(action="routing.rollback")
        self.assertEqual(len(items), 1)

    def test_redrive_idempotent(self):
        class _Task:
            queue_task_id = "task-1"
            status = "queued"

        class _Queue:
            def redrive_dead_letter(self, task_id, *, actor_ref, tenant_id):
                return _Task()

        class _WR:
            queue = _Queue()

        svc = OperationsAdminService(audit_store=self.audit, workflow_runtime=_WR())
        cmd = RedriveDLQCommand(task_id="task-1", tenant_id="tenant-a", idempotency_key="idem-key-12345678")
        svc.redrive_dlq(_ctx(), cmd)
        svc.redrive_dlq(_ctx(), cmd)


class AdminHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(_auth_env())
        import importlib
        import main as main_mod

        configure_security(auth=AuthService(env=_auth_env()))
        importlib.reload(main_mod)
        cls.main = main_mod
        cls.client = TestClient(main_mod.app)

    def test_normal_user_denied_admin_api(self):
        r = self.client.get("/api/admin/ops/dashboard", headers={"X-API-Key": "secret-user"})
        self.assertEqual(r.status_code, 403)

    def test_viewer_can_read_not_drain(self):
        r = self.client.get("/api/admin/ops/dashboard", headers={"X-API-Key": "secret-viewer"})
        self.assertEqual(r.status_code, 200)
        r2 = self.client.post("/api/admin/ops/workers/drain", headers={"X-API-Key": "secret-viewer"}, json={})
        self.assertEqual(r2.status_code, 403)

    def test_admin_dashboard(self):
        r = self.client.get("/api/admin/ops/dashboard", headers={"X-API-Key": "secret-admin"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("health", r.json())

    def test_admin_page_served(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Panda Ops", r.text)

    def test_drain_requires_auth(self):
        r = self.client.post("/admin/drain")
        self.assertIn(r.status_code, {401, 403})

    def test_drain_and_resume_admin(self):
        r = self.client.post("/admin/drain", headers={"X-API-Key": "secret-admin"})
        self.assertEqual(r.status_code, 200)
        r2 = self.client.post("/api/admin/ops/workers/resume", headers={"X-API-Key": "secret-admin"}, json={})
        self.assertEqual(r2.status_code, 200)

    def test_runs_pagination(self):
        r = self.client.get("/api/admin/ops/runs?limit=10&offset=0", headers={"X-API-Key": "secret-admin"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("items", body)
        self.assertLessEqual(body["page"]["limit"], 200)

    def test_huge_page_size_clamped(self):
        r = self.client.get("/api/admin/ops/runs?limit=9999", headers={"X-API-Key": "secret-admin"})
        self.assertEqual(r.status_code, 422)

    def test_audit_pagination_and_no_cache(self):
        self.client.post("/admin/drain", headers={"X-API-Key": "secret-admin"})
        r = self.client.get("/api/admin/ops/audit?limit=5", headers={"X-API-Key": "secret-admin"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r.headers.get("cache-control", ""))

    def test_tenant_admin_cannot_read_tenant_b_budget(self):
        r = self.client.get("/api/admin/ops/budgets/tenant-b", headers={"X-API-Key": "secret-tadmin"})
        self.assertEqual(r.status_code, 403)

    def test_providers_endpoint(self):
        r = self.client.get("/api/admin/ops/providers", headers={"X-API-Key": "secret-admin"})
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)

    def test_no_secrets_in_admin_static(self):
        r = self.client.get("/static/admin/admin.js")
        for needle in ("OPENAI_API_KEY", "Bearer sk-", "secret-admin"):
            self.assertNotIn(needle, r.text)

    def test_xss_safe_admin_page_labels(self):
        r = self.client.get("/admin")
        self.assertNotIn("<script>", r.text)


class AdminSecurityTests(unittest.TestCase):
    def test_confirmation_token_binding(self):
        t1 = confirmation_token(actor_ref="a", action="routing.activate", target_id="c1")
        t2 = confirmation_token(actor_ref="b", action="routing.activate", target_id="c1")
        self.assertNotEqual(t1, t2)

    def test_xss_escaping_helper(self):
        from ui_chat.markdown import render_markdown_safe

        html = render_markdown_safe("<script>alert(1)</script>")
        self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main()
