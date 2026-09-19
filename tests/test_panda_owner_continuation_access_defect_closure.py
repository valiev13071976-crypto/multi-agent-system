"""PANDA -- BAA_ACCESS_DENIED production defect closure: an authenticated
OWNER continuing their OWN conversation (same conversation, same freshly
uploaded XLSX, upload/analyze already succeeded) must be able to submit an
ordinary attachment-less follow-up turn and read its status/result without
ever hitting ``BAA_ACCESS_DENIED`` -- while a genuinely DIFFERENT owner (or
tenant) reading someone else's request must still be denied exactly as
before.

READ-ONLY AUDIT FINDING (this task): the production path from conversation
-> attachment/upload -> request submission -> request/status/result access
-> owner_id/tenant_id propagation -> authorization comparison has exactly
ONE ``BAA_ACCESS_DENIED`` raise site in the whole codebase --
``BusinessAssistantApiService.get_request``'s ``rec.owner_id != owner_id``
check (business_assistant_api/service.py). Two concrete, generic gaps feed
a DIFFERENT owner's data into that check for a caller who never asked for
it, so a caller's OWN just-returned request_id/conversation_id can start
failing that owner check even though the caller never changed identity:

  1. ``_submit_prepare``'s idempotency-key short-circuit
     (``store.get_request_by_idempotency``) was scoped by
     ``(tenant_id, idempotency_key)`` only -- never checked against the
     CALLING owner_id. A same-tenant idempotency_key + payload collision
     from a different owner used to hand that caller back the OTHER
     owner's ``ApiRequestRecord`` with a 200 OK at submit time; the very
     next status/result poll for that SAME (foreign) request_id then hit
     ``BAA_ACCESS_DENIED`` for the caller who was just given it.
  2. ``_ensure_conversation`` looked up the conversation OWNER-SCOPED, but
     ``ba_api_conversations``'s primary key is ``conversation_id`` alone
     (``conversation_id`` is client-supplied per ``SubmitRequestBody``,
     with no format/uniqueness validation). A caller whose owner_id did
     not match an ALREADY-persisted conversation_id's owner fell through
     to ``INSERT OR REPLACE`` -- silently REASSIGNING that conversation's
     ownership to themselves. The conversation's true original owner then
     no longer matched on their own next turn either.

Both are now fail-closed: BOTH raise ``BAA_ACCESS_DENIED``/
``BAA_IDEMPOTENCY_CONFLICT`` for the mismatched caller instead of silently
returning or reassigning another owner's data -- never weakening the
existing ``get_request`` owner check, never bypassing owner/tenant
isolation, never touching product-resolver logic.

Security invariant covered end to end:
  A. same owner, continuation AFTER an attachment turn -> allowed.
  B. same owner, second turn with attachment_count=0 (arbitrary product
     text, no attachment, existing conversation) -> allowed.
  C. a DIFFERENT owner (same tenant) reading someone else's request ->
     denied (BAA_ACCESS_DENIED, 403).
  D. a DIFFERENT tenant reading someone else's request -> denied (never
     200, never leaks the record).
  Plus direct unit coverage of the two closed gaps themselves (idempotency
  cross-owner collision, conversation_id cross-owner hijack).
"""

from __future__ import annotations

import importlib
import io
import os
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient


class _HttpJourneyBase(unittest.TestCase):
    """Shared REAL ``main.app`` HTTP harness (session-cookie auth, the exact
    production auth shape the defect report describes)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_api.sqlite")
        self.upload_dir = os.path.join(self.tmp, "uploads")
        self.accounts_db = os.path.join(self.tmp, "accounts.sqlite")
        os.makedirs(self.upload_dir, exist_ok=True)

        self._old_env = {
            k: os.environ.get(k)
            for k in (
                "BA_API_DB_PATH",
                "BA_API_UPLOAD_DIR",
                "ACCOUNTS_DB_PATH",
                "SECURITY_AUTH_MODE",
                "PANDA_API_KEYS",
                "PANDA_MANAGED_AGENT_ENABLED",
            )
        }
        os.environ["BA_API_DB_PATH"] = self.db
        os.environ["BA_API_UPLOAD_DIR"] = self.upload_dir
        os.environ["ACCOUNTS_DB_PATH"] = self.accounts_db
        os.environ["SECURITY_AUTH_MODE"] = "required"
        os.environ["PANDA_API_KEYS"] = "unused-key|unused-tenant|unused-user|user|unused-secret"
        os.environ.pop("PANDA_MANAGED_AGENT_ENABLED", None)

        import main as main_mod

        self.main = importlib.reload(main_mod)
        self.client = TestClient(self.main.app)

    def tearDown(self):
        try:
            self.main.ba_api_runtime.close()
        except Exception:
            pass
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_and_login(self, username: str, *, tenant_id: str | None = None) -> dict:
        payload = {
            "username": username,
            "password": "SuperSecret123!",
            "accept_terms": True,
            "accept_privacy": True,
        }
        if tenant_id:
            payload["tenant_id"] = tenant_id
        reg = self.client.post("/api/accounts/register", json=payload)
        self.assertEqual(reg.status_code, 200, reg.text)
        login = self.client.post(
            "/api/accounts/login",
            json={"username": username, "password": "SuperSecret123!"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        return reg.json()

    def _new_session_client(self) -> TestClient:
        """A second, independent cookie jar over the SAME app -- models a
        genuinely different browser/session for a second identity."""
        return TestClient(self.main.app)

    def _xlsx_bytes(self) -> bytes:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["sku", "product_name", "purchase_price"])
        ws.append(["SKU-1", "Generic Widget", "1000"])
        ws.append(["SKU-2", "Generic Gadget", "2000"])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _poll_terminal_status(self, client: TestClient, request_id: str) -> dict:
        last = {}
        for _ in range(20):
            st = client.get(f"/api/v1/business-assistant/requests/{request_id}/status")
            self.assertEqual(st.status_code, 200, st.text)
            last = st.json()
            if last.get("status") in {"COMPLETED", "FAILED"}:
                break
        return last


class SameOwnerContinuationAllowedTests(_HttpJourneyBase):
    """Tests A + B: the SAME authenticated owner, SAME conversation, SAME
    freshly uploaded XLSX -- upload/analyze succeeds, then an ordinary
    attachment-less follow-up in that SAME conversation must also succeed,
    with zero 401/403/``BAA_ACCESS_DENIED`` anywhere in the chain. Uses an
    arbitrary product-text fixture (never bound to any specific brand/
    model/SKU)."""

    def test_a_same_owner_continuation_after_attachment_allowed(self):
        self._register_and_login("panda-owner-a")
        conv = self.client.post(
            "/api/v1/business-assistant/conversations", json={"title": "Price review"}
        )
        self.assertEqual(conv.status_code, 200, conv.text)
        conversation_id = conv.json()["conversation_id"]

        up = self.client.post(
            "/api/v1/business-assistant/attachments",
            files={
                "file": (
                    "price_list.xlsx",
                    io.BytesIO(self._xlsx_bytes()),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        self.assertEqual(up.status_code, 200, up.text)
        artifact_ref = up.json()["artifact_ref"]

        req1 = self.client.post(
            "/api/v1/business-assistant/requests",
            json={
                "message": "Проанализируй этот прайс.",
                "artifact_refs": [artifact_ref],
                "conversation_id": conversation_id,
                "idempotency_key": "owner-a-turn-1-key",
            },
        )
        self.assertEqual(req1.status_code, 200, req1.text)
        request_id_1 = req1.json()["request_id"]

        status1 = self._poll_terminal_status(self.client, request_id_1)
        self.assertNotEqual(status1.get("status"), "FAILED", status1)

        result1 = self.client.get(f"/api/v1/business-assistant/requests/{request_id_1}/result")
        self.assertEqual(result1.status_code, 200, result1.text)
        self.assertNotIn(result1.status_code, (401, 403))

    def test_b_same_owner_second_request_no_attachment_allowed(self):
        """Test B: attachment_count=0 for turn 2, but the conversation
        already carries a workset/active task from turn 1's upload+
        analyze -- must still be allowed for the SAME owner."""
        self._register_and_login("panda-owner-b")
        conv = self.client.post(
            "/api/v1/business-assistant/conversations", json={"title": "Price review"}
        )
        self.assertEqual(conv.status_code, 200, conv.text)
        conversation_id = conv.json()["conversation_id"]

        up = self.client.post(
            "/api/v1/business-assistant/attachments",
            files={
                "file": (
                    "price_list.xlsx",
                    io.BytesIO(self._xlsx_bytes()),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        self.assertEqual(up.status_code, 200, up.text)
        artifact_ref = up.json()["artifact_ref"]

        req1 = self.client.post(
            "/api/v1/business-assistant/requests",
            json={
                "message": "Проанализируй этот прайс.",
                "artifact_refs": [artifact_ref],
                "conversation_id": conversation_id,
                "idempotency_key": "owner-b-turn-1-key",
            },
        )
        self.assertEqual(req1.status_code, 200, req1.text)
        request_id_1 = req1.json()["request_id"]
        self._poll_terminal_status(self.client, request_id_1)
        result1 = self.client.get(f"/api/v1/business-assistant/requests/{request_id_1}/result")
        self.assertNotIn(result1.status_code, (401, 403))

        # SAME conversation, SAME owner, arbitrary product-text fixture,
        # NO new attachment_refs (attachment_count=0) -- the exact
        # reported production shape ("Покажи <arbitrary product text>").
        req2 = self.client.post(
            "/api/v1/business-assistant/requests",
            json={
                "message": "Покажи Generic Gadget",
                "conversation_id": conversation_id,
                "idempotency_key": "owner-b-turn-2-key",
            },
        )
        self.assertEqual(req2.status_code, 200, req2.text)
        self.assertNotIn(req2.status_code, (401, 403))
        request_id_2 = req2.json()["request_id"]
        self.assertNotEqual(req2.json().get("error_code"), "BAA_ACCESS_DENIED", req2.text)

        status2 = self._poll_terminal_status(self.client, request_id_2)
        self.assertNotEqual(status2.get("error_code"), "BAA_ACCESS_DENIED", status2)

        result2 = self.client.get(f"/api/v1/business-assistant/requests/{request_id_2}/result")
        self.assertNotIn(result2.status_code, (401, 403), result2.text)

        refetch2 = self.client.get(f"/api/v1/business-assistant/requests/{request_id_2}")
        self.assertEqual(refetch2.status_code, 200, refetch2.text)


class DifferentOwnerAndTenantDeniedTests(_HttpJourneyBase):
    """Tests C + D: the security invariant this task must NEVER weaken --
    a genuinely different owner (C) or different tenant (D) must still be
    denied access to someone else's request.

    Self-registration (``POST /api/accounts/register``) always issues a
    brand-new, randomly generated ``tenant_id`` and silently ignores any
    client-supplied one (``accounts/router.py`` never forwards
    ``RegisterRequest.tenant_id`` to ``AccountsService.register`` --
    correct, deliberate isolation: letting a self-registering caller pick
    an arbitrary existing tenant to join would itself be a cross-tenant
    defect). Multi-user-per-tenant provisioning is an OWNER-only
    (``owner_create_user``) capability this task must not touch. So
    "different owner, same tenant" is driven directly against the SAME
    real, HTTP-wired ``BusinessAssistantApiService`` instance the app
    uses for every other endpoint -- exercising the EXACT unmodified
    ``get_request`` owner check production requests go through, without
    inventing a second account-provisioning path."""

    def test_c_different_owner_same_tenant_denied(self):
        reg1 = self._register_and_login("panda-owner-c1")
        real_tenant = reg1["tenant_id"]
        conv = self.client.post(
            "/api/v1/business-assistant/conversations", json={"title": "Owner 1 chat"}
        )
        self.assertEqual(conv.status_code, 200, conv.text)
        conversation_id = conv.json()["conversation_id"]
        req = self.client.post(
            "/api/v1/business-assistant/requests",
            json={
                "message": "Проанализируй этот прайс.",
                "conversation_id": conversation_id,
                "idempotency_key": "owner-c1-key-0001",
            },
        )
        self.assertEqual(req.status_code, 200, req.text)
        request_id = req.json()["request_id"]

        from business_assistant_api.errors import BAA_ACCESS_DENIED, BusinessAssistantApiError

        with self.assertRaises(BusinessAssistantApiError) as ctx:
            self.main.ba_api_runtime.service.get_request(
                tenant_id=real_tenant, owner_id="a-different-owner-same-tenant", request_id=request_id
            )
        self.assertEqual(ctx.exception.code, BAA_ACCESS_DENIED)
        self.assertEqual(ctx.exception.http_status, 403)

        # The legitimate owner must remain unaffected by the denied
        # cross-owner attempt.
        still_ok = self.client.get(f"/api/v1/business-assistant/requests/{request_id}")
        self.assertEqual(still_ok.status_code, 200, still_ok.text)

    def test_d_different_tenant_denied(self):
        reg1 = self._register_and_login("panda-owner-d1")
        conv = self.client.post(
            "/api/v1/business-assistant/conversations", json={"title": "Tenant 1 chat"}
        )
        self.assertEqual(conv.status_code, 200, conv.text)
        conversation_id = conv.json()["conversation_id"]
        req = self.client.post(
            "/api/v1/business-assistant/requests",
            json={
                "message": "Проанализируй этот прайс.",
                "conversation_id": conversation_id,
                "idempotency_key": "owner-d1-key-0001",
            },
        )
        self.assertEqual(req.status_code, 200, req.text)
        request_id = req.json()["request_id"]

        from business_assistant_api.errors import BusinessAssistantApiError

        with self.assertRaises(BusinessAssistantApiError) as ctx:
            self.main.ba_api_runtime.service.get_request(
                tenant_id="a-completely-different-tenant",
                owner_id=reg1["user_id"],
                request_id=request_id,
            )
        # Never leaks the record's existence across tenants -- BAA_NOT_FOUND,
        # never a 200, never the other tenant's data.
        self.assertNotEqual(ctx.exception.http_status, 200)
        self.assertIn(ctx.exception.http_status, (403, 404))


class OwnershipGapUnitTests(unittest.TestCase):
    """Direct unit coverage of the two specific gaps closed by this task,
    isolated from the full HTTP/model stack."""

    def setUp(self):
        import tempfile as _tempfile

        from business_assistant.service import BusinessAssistantService
        from business_assistant_api.service import BusinessAssistantApiService
        from business_assistant_api.store import SqliteBusinessAssistantApiStore

        self.tmp = _tempfile.mkdtemp()
        self.store = SqliteBusinessAssistantApiStore(os.path.join(self.tmp, "ba.sqlite"))
        self.svc = BusinessAssistantApiService(
            ba_service=BusinessAssistantService(), store=self.store
        )
        self.tenant = "tenant-unit"

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idempotency_collision_across_owners_is_denied_not_leaked(self):
        from business_assistant_api.errors import BAA_IDEMPOTENCY_CONFLICT, BusinessAssistantApiError

        rec_a = self.svc.submit(
            tenant_id=self.tenant,
            owner_id="owner-A",
            message="Проанализируй этот прайс.",
            idempotency_key="shared-key-unit-0001",
        )
        self.assertEqual(rec_a.owner_id, "owner-A")

        with self.assertRaises(BusinessAssistantApiError) as ctx:
            self.svc.submit(
                tenant_id=self.tenant,
                owner_id="owner-B",
                message="Проанализируй этот прайс.",
                idempotency_key="shared-key-unit-0001",
            )
        self.assertEqual(ctx.exception.code, BAA_IDEMPOTENCY_CONFLICT)
        self.assertEqual(ctx.exception.http_status, 409)

        # Owner A's own record must remain intact and still owner-A's.
        still_a = self.svc.get_request(
            tenant_id=self.tenant, owner_id="owner-A", request_id=rec_a.request_id
        )
        self.assertEqual(still_a.owner_id, "owner-A")

    def test_same_owner_reusing_own_idempotency_key_same_payload_still_allowed(self):
        rec_a = self.svc.submit(
            tenant_id=self.tenant,
            owner_id="owner-A",
            message="Проанализируй этот прайс.",
            idempotency_key="owner-a-retry-key-0001",
        )
        rec_a_retry = self.svc.submit(
            tenant_id=self.tenant,
            owner_id="owner-A",
            message="Проанализируй этот прайс.",
            idempotency_key="owner-a-retry-key-0001",
        )
        self.assertEqual(rec_a.request_id, rec_a_retry.request_id)
        self.assertEqual(rec_a_retry.owner_id, "owner-A")

    def test_conversation_id_hijack_by_different_owner_is_denied(self):
        from business_assistant_api.errors import BAA_ACCESS_DENIED, BusinessAssistantApiError

        conv = self.svc.create_conversation(tenant_id=self.tenant, owner_id="owner-A", title="t")
        cid = conv.conversation_id

        with self.assertRaises(BusinessAssistantApiError) as ctx:
            self.svc.submit(
                tenant_id=self.tenant,
                owner_id="owner-B",
                message="Проанализируй этот прайс.",
                conversation_id=cid,
            )
        self.assertEqual(ctx.exception.code, BAA_ACCESS_DENIED)
        self.assertEqual(ctx.exception.http_status, 403)

        # The conversation's original ownership must remain untouched --
        # never silently reassigned to the mismatched caller.
        untouched = self.store.get_conversation(
            tenant_id=self.tenant, owner_id="owner-A", conversation_id=cid
        )
        self.assertIsNotNone(untouched)
        self.assertEqual(untouched.owner_id, "owner-A")

    def test_same_owner_reusing_own_conversation_id_still_allowed(self):
        conv = self.svc.create_conversation(tenant_id=self.tenant, owner_id="owner-A", title="t")
        cid = conv.conversation_id

        # Same owner submitting a second, third, ... turn in their OWN
        # conversation must never be denied by the new cross-owner check.
        rec = self.svc.submit(
            tenant_id=self.tenant,
            owner_id="owner-A",
            message="Проанализируй этот прайс.",
            conversation_id=cid,
        )
        self.assertEqual(rec.conversation_id, cid)
        fetched = self.svc.get_request(
            tenant_id=self.tenant, owner_id="owner-A", request_id=rec.request_id
        )
        self.assertEqual(fetched.owner_id, "owner-A")


if __name__ == "__main__":
    unittest.main()
