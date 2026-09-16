"""PANDA -- fresh-owner HTTP upload/request/status/result journey, driven
through the REAL ``main.app`` (the exact attachment -> request -> polling
-> result chain production goes through), closing out the remaining
transport-level steps of the "ONE authoritative execution route" task:

  - artifact_ref filenames containing spaces/parentheses/Cyrillic (PR #95's
    opaque ``artifact://upload/{upload_id}`` contract) must upload AND
    then be accepted by ``POST /requests`` end to end, not just at the
    two-function unit-contract level already covered by
    ``tests.test_panda_selected_product_scope_continuity_defect_closure.
    ArtifactRefFilenameContractTests``;
  - a genuinely FRESH, self-registered owner (session-cookie auth, the
    exact "И вы нет доступа к этой функции"/``BAA_ACCESS_DENIED`` defect
    report's own auth shape -- never a pre-provisioned ``PANDA_API_KEYS``
    tenant) must be able to upload -> submit -> poll -> read back its OWN
    request without ever hitting ``BAA_ACCESS_DENIED``, across an
    ordinary conversational turn AND an Excel-attached turn;
  - plain, non-Excel conversation ("how are you") must keep working
    without being forced into Data Intelligence/Workset routing.

``PANDA_MANAGED_AGENT_ENABLED`` stays at its production default (unset =
disabled) for this module, so every Excel turn below is resolved by the
existing deterministic ``compile_request`` legacy path -- zero model
calls, zero managed-agent subprocess, fully deterministic. Zero live
Bitrix (no Bitrix credentials/activation are configured for this
runtime, so no write tool is even reachable).
"""

from __future__ import annotations

import importlib
import io
import os
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

PRODUCTION_FILENAMES = [
    "TCL (1).xlsx",
    "Прайс TCL сентябрь.xlsx",
    "Прайс сентябрь (финал).xlsx",
]


class FreshOwnerHttpUploadRequestAccessJourneyTests(unittest.TestCase):
    """Steps 19-22 of the mandatory acceptance journey (upload with
    production-shaped filenames -> request -> status -> result, with zero
    ``BAA_ACCESS_DENIED`` for the SAME authenticated fresh owner), plus a
    plain-conversation smoke check (step: "normal chat must still
    work")."""

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
        # ``AuthService.require_production_keys()`` fails fast with an
        # empty key set regardless of auth path used per-request -- this
        # dummy, unrelated tenant/key is never used by this journey
        # (every request below authenticates via session cookie only),
        # it only satisfies that unconditional boot-time invariant.
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

    def _register_and_login(self, username: str) -> None:
        reg = self.client.post(
            "/api/accounts/register",
            json={
                "username": username,
                "password": "SuperSecret123!",
                "accept_terms": True,
                "accept_privacy": True,
            },
        )
        self.assertEqual(reg.status_code, 200, reg.text)
        login = self.client.post(
            "/api/accounts/login",
            json={"username": username, "password": "SuperSecret123!"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        me = self.client.get("/api/accounts/me")
        self.assertEqual(me.status_code, 200, me.text)

    def _xlsx_bytes(self) -> bytes:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["sku", "product_name", "purchase_price"])
        ws.append(["SKU-1", "Товар 1", "1000"])
        ws.append(["SKU-2", "Товар 2", "2000"])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _poll_terminal_status(self, request_id: str) -> dict:
        last = {}
        for _ in range(20):
            st = self.client.get(f"/api/v1/business-assistant/requests/{request_id}/status")
            self.assertEqual(st.status_code, 200, st.text)
            last = st.json()
            if last.get("status") in {"COMPLETED", "FAILED"}:
                break
        return last

    def test_fresh_owner_normal_chat_never_returns_access_denied(self):
        """A plain, non-business turn for a fresh owner must never fail
        with 401/403 (the reported ``BAA_ACCESS_DENIED``/"У вас нет
        доступа к этой функции" defect). This module's OWN Excel/Product/
        Bitrix canonical-routing changes never touch the general
        conversational (non-Excel) reply pipeline at all -- see
        ``CanonicalRoutingNeverHijacksNonBusinessChatTests`` in
        ``tests.test_panda_canonical_single_authority_orchestration`` for
        a deterministic, mocked-model proof that a plain chat turn is
        never routed into Data Intelligence/Workset/managed-agent. This
        HTTP-level check only proves the ACCESS boundary (never the
        general AI-core provider pipeline's own success, which depends on
        this sandbox's live model-provider quota/availability and is
        unrelated to this task's scope: this sandbox's general Panda-AI-
        Core chat pipeline can independently return a 503 ``BAA_
        CONVERSATION_UNAVAILABLE`` when its own (unrelated, pre-existing)
        multi-provider expert pool has no available provider -- never a
        401/403, and never anything this task's canonical-Workset changes
        could cause)."""
        self._register_and_login("freshchat1")
        conv = self.client.post(
            "/api/v1/business-assistant/conversations", json={"title": "Just chatting"}
        )
        self.assertEqual(conv.status_code, 200, conv.text)
        req = self.client.post(
            "/api/v1/business-assistant/requests",
            json={
                "message": "Привет! Как дела?",
                "conversation_id": conv.json()["conversation_id"],
                "idempotency_key": "fresh-chat-1",
            },
        )
        self.assertNotIn(req.status_code, (401, 403), req.text)
        if req.status_code == 200:
            request_id = req.json()["request_id"]
            summary = self.client.get(f"/api/v1/business-assistant/requests/{request_id}")
            self.assertNotIn(summary.status_code, (401, 403), summary.text)

    def test_fresh_owner_upload_request_status_result_no_access_denied(self):
        """Steps 19-21: two production-shaped filenames, each carried all
        the way through upload -> request -> status -> result -> re-fetch
        for the SAME fresh, session-authenticated owner, with zero
        ``BAA_ACCESS_DENIED``/401/403 anywhere in the chain."""
        self._register_and_login("freshupload1")
        xlsx_bytes = self._xlsx_bytes()

        conv = self.client.post(
            "/api/v1/business-assistant/conversations", json={"title": "Price review"}
        )
        self.assertEqual(conv.status_code, 200, conv.text)
        conversation_id = conv.json()["conversation_id"]

        for index, filename in enumerate(PRODUCTION_FILENAMES[:2]):
            with self.subTest(filename=filename):
                up = self.client.post(
                    "/api/v1/business-assistant/attachments",
                    files={
                        "file": (
                            filename,
                            io.BytesIO(xlsx_bytes),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                )
                self.assertEqual(up.status_code, 200, up.text)
                artifact_ref = up.json()["artifact_ref"]
                self.assertTrue(artifact_ref)

                req = self.client.post(
                    "/api/v1/business-assistant/requests",
                    json={
                        "message": "Проанализируй этот прайс.",
                        "artifact_refs": [artifact_ref],
                        "conversation_id": conversation_id,
                        "idempotency_key": f"fresh-upload-{index}",
                    },
                )
                # DEFECT 5 (artifact_ref filename contract): must never be
                # rejected as artifact_ref_invalid for a normal filename.
                self.assertEqual(req.status_code, 200, req.text)
                request_id = req.json()["request_id"]

                status = self._poll_terminal_status(request_id)
                self.assertNotEqual(status.get("status"), "FAILED", status)

                result = self.client.get(
                    f"/api/v1/business-assistant/requests/{request_id}/result"
                )
                # The reported production defect: BAA_ACCESS_DENIED /
                # "У вас нет доступа к этой функции" for the SAME
                # authenticated owner reading back its OWN request.
                self.assertNotIn(result.status_code, (401, 403), result.text)

                refetch = self.client.get(f"/api/v1/business-assistant/requests/{request_id}")
                self.assertEqual(refetch.status_code, 200, refetch.text)
                self.assertNotIn(refetch.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
