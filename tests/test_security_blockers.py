"""Tests for the three Security blockers: persistence tenant, HITL HTTP, public rate limit."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from documents.models import DocumentIngestRequest
from documents.service import DocumentService
from documents.sqlite_store import SqliteDocumentStore
from hitl.authority import InMemoryApprovalAuthority
from hitl.models import APPROVAL_CLASS_STANDARD
from hitl.service import HITLService
from memory.models import (
    MEMORY_SEMANTIC,
    MemoryIngestRequest,
    MemoryScope,
    SOURCE_OPERATOR,
    utc_now,
)
from memory.service import MemoryService
from memory.sqlite_store import SqliteMemoryStore
from security.config import DEFAULT_LEGACY_TENANT, ROLE_APPROVER, ROLE_USER
from security.hitl_auth import HitlActionPayload, HitlHttpAuthorizer, hitl_role_from_rbac
from security.identity import RequestSecurityContext
from security.rate_limit import RateLimiter
from security.api_auth import PublicRateLimitMiddleware, _client_ip
from workflow.state_manager import StateManager


def _scope(tenant: str, sid: str = "ws1") -> MemoryScope:
    return MemoryScope(scope_type="workspace", scope_id=sid, tenant_ref=tenant)


class MemoryTenantPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SqliteMemoryStore(db_path=self.tmp.name)
        self.svc = MemoryService(self.store)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def _ingest(self, tenant: str, content: str, sid: str = "ws1"):
        return self.svc.ingest(
            MemoryIngestRequest(
                scope=_scope(tenant, sid),
                memory_type=MEMORY_SEMANTIC,
                content=content,
                source_type=SOURCE_OPERATOR,
                source_id="op-1",
            ),
            requesting_scope=_scope(tenant, sid),
        )

    def test_tenant_b_store_query_returns_empty_for_tenant_a_row(self):
        rec = self._ingest("tenant-a", "secret fact for tenant a")
        rows = self.store.list_by_scope(_scope("tenant-b", "ws1"))
        self.assertEqual(rows, ())
        got = self.store.get(rec.memory_id, scope=_scope("tenant-b", "ws1"))
        self.assertIsNone(got)

    def test_cross_tenant_get_delete_denied_at_store(self):
        rec = self._ingest("tenant-a", "isolated")
        self.assertIsNone(self.store.get(rec.memory_id, scope=_scope("tenant-b")))
        with self.assertRaises(Exception):
            self.svc.forget(rec.memory_id, requesting_scope=_scope("tenant-b"))

    def test_legacy_default_scoped_only(self):
        legacy = MemoryScope(scope_type="workspace", scope_id="legacy-ws")
        rec = self.svc.ingest(
            MemoryIngestRequest(
                scope=legacy,
                memory_type=MEMORY_SEMANTIC,
                content="legacy row",
                source_type=SOURCE_OPERATOR,
                source_id="op-legacy",
            ),
            requesting_scope=MemoryScope(
                scope_type="workspace",
                scope_id="legacy-ws",
                tenant_ref=DEFAULT_LEGACY_TENANT,
            ),
        )
        other = self.store.list_by_scope(_scope("tenant-x", "legacy-ws"))
        self.assertEqual(other, ())
        found = self.store.list_by_scope(
            MemoryScope(
                scope_type="workspace",
                scope_id="legacy-ws",
                tenant_ref=DEFAULT_LEGACY_TENANT,
            )
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].memory_id, rec.memory_id)

    def test_dedup_does_not_cross_tenant(self):
        a = self._ingest("tenant-a", "same content", "shared-id")
        b = self._ingest("tenant-b", "same content", "shared-id")
        self.assertNotEqual(a.memory_id, b.memory_id)


class DocumentTenantPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SqliteDocumentStore(db_path=self.tmp.name)
        self.svc = DocumentService(self.store)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def _ingest(self, tenant: str, content: bytes = b"doc body"):
        scope = _scope(tenant)
        return self.svc.ingest(
            DocumentIngestRequest(
                scope=scope,
                filename="test.txt",
                content=content,
                source_type="operator",
                source_id="src-1",
                ingested_by="tester",
            ),
            requesting_scope=scope,
        )

    def test_tenant_b_cannot_list_tenant_a_documents(self):
        self._ingest("tenant-a")
        rows = self.store.list_by_scope(_scope("tenant-b"))
        self.assertEqual(rows, ())

    def test_cross_tenant_get_returns_none(self):
        rec = self._ingest("tenant-a")
        self.assertIsNone(self.store.get(rec.document_id, scope=_scope("tenant-b")))


class HitlHttpBindingTests(unittest.TestCase):
    def _ctx(self, tenant: str, user: str, roles: tuple[str, ...]) -> RequestSecurityContext:
        return RequestSecurityContext(
            user_id=user,
            tenant_id=tenant,
            roles=roles,
            request_id="req-1",
        )

    def test_admin_alone_cannot_map_to_hitl_role(self):
        self.assertIsNone(hitl_role_from_rbac(("admin",)))

    def test_approver_maps_to_hitl_role(self):
        self.assertIsNotNone(hitl_role_from_rbac((ROLE_APPROVER,)))

    def test_payload_tenant_mismatch_denied(self):
        from security.errors import ResourceNotFoundError
        from security.hitl_auth import _validate_payload_against_context

        ctx = self._ctx("tenant-a", "u1", (ROLE_APPROVER,))
        with self.assertRaises(ResourceNotFoundError):
            _validate_payload_against_context(
                ctx, HitlActionPayload(tenant_id="tenant-b")
            )

    def test_payload_forged_approver_denied(self):
        from security.errors import UnauthorizedError
        from security.hitl_auth import _validate_payload_against_context

        ctx = self._ctx("tenant-a", "u1", (ROLE_APPROVER,))
        with self.assertRaises(UnauthorizedError):
            _validate_payload_against_context(
                ctx, HitlActionPayload(approver_id="attacker")
            )


class PublicRateLimitTests(unittest.TestCase):
    def test_health_within_limit(self):
        limiter = RateLimiter(health_limit=5, ip_limit=2, window_seconds=60.0)
        for _ in range(5):
            limiter.check_health(source_ip="10.0.0.1")

    def test_public_over_limit_429(self):
        from security.errors import RateLimitedError

        limiter = RateLimiter(ip_limit=2, window_seconds=60.0)
        limiter.check_unauthenticated(source_ip="10.0.0.2")
        limiter.check_unauthenticated(source_ip="10.0.0.2")
        with self.assertRaises(RateLimitedError):
            limiter.check_unauthenticated(source_ip="10.0.0.2")

    def test_different_ip_buckets_separate(self):
        limiter = RateLimiter(ip_limit=1, window_seconds=60.0)
        limiter.check_unauthenticated(source_ip="10.0.0.3")
        limiter.check_unauthenticated(source_ip="10.0.0.4")

    def test_health_endpoint_works(self):
        from tests.test_smoke import load_app

        main_mod = load_app()
        client = TestClient(main_mod.app)
        for _ in range(3):
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)


class HitlHttpIntegrationTests(unittest.TestCase):
    def test_cross_tenant_approval_http_404(self):
        from tests.test_smoke import load_app

        main_mod = load_app(
            SECURITY_AUTH_MODE="required",
            PANDA_API_KEYS=(
                "ka|tenant-a|ua|user,operator|sec-a;"
                "kb|tenant-b|ub|approver|sec-b"
            ),
        )
        client = TestClient(main_mod.app)
        created = client.post(
            "/api/workflows",
            json={"workflow_type": "demo.linear", "version": "1", "sync": True},
            headers={"X-API-Key": "sec-a"},
        )
        self.assertEqual(created.status_code, 200)
        wid = created.json()["workflow_id"]
        denied = client.post(
            f"/api/workflows/{wid}/approvals/fake-approval/approve",
            json={"tenant_id": "tenant-a", "approver_role": "approver"},
            headers={"X-API-Key": "sec-b"},
        )
        self.assertIn(denied.status_code, {403, 404})


if __name__ == "__main__":
    unittest.main()
