"""Block 4.38 — realtime WebSocket authentication.

Proves the realtime `/api/v1/realtime/ws` transport uses the SAME dual-auth
precedence (session cookie for human web sessions, then X-API-Key/bearer for
machine/workspace keys) as every other HTTP endpoint
(accounts.dual_auth.get_security_context_dual) -- no relaxed realtime-only
auth path, fails closed with no credential at all. Offline/fake-provider
only (no live audio hardware, no paid STT/TTS/LLM calls).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from accounts.dual_auth import configure_accounts_auth, install_dual_auth
from accounts.models import ROLE_OWNER
from accounts.router import configure_accounts_router
from accounts.runtime import build_accounts_runtime
from business_assistant_api.runtime import build_business_assistant_api_runtime
from realtime.bridge import RealtimeConversationBridge
from realtime.router import configure_realtime_router
from security.api_auth import configure_security
from security.auth import AuthService
from ui_chat.voice.stt import FakeSpeechToTextProvider
from ui_chat.voice.tts import FakeTextToSpeechProvider


class RealtimeWsAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.accounts_runtime = build_accounts_runtime(
            env={"ACCOUNTS_DB_PATH": os.path.join(self.tmp, "accounts.sqlite")}
        )
        self.ba = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "ba.sqlite"), with_integration=False
        )
        configure_security(
            auth=AuthService(
                env={
                    "SECURITY_AUTH_MODE": "required",
                    "PANDA_API_KEYS": "key-a|tenant-o1|user-a|user|secret-a",
                }
            )
        )
        configure_accounts_auth(self.accounts_runtime.service)
        install_dual_auth()

        self.bridge = RealtimeConversationBridge(
            ba_api=self.ba.service, stt=FakeSpeechToTextProvider(), tts=FakeTextToSpeechProvider()
        )
        app = FastAPI()
        app.include_router(configure_accounts_router(self.accounts_runtime.service))
        app.include_router(configure_realtime_router(self.bridge))
        self.client = TestClient(app)
        self.accounts_runtime.service.identity.create_user(
            username="owner1",
            password="OwnerPass12!",
            tenant_id="tenant-o1",
            role=ROLE_OWNER,
            actor_id="bootstrap",
            is_bootstrap_owner=True,
            protected=True,
        )

    def tearDown(self):
        self.accounts_runtime.close()
        self.ba.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_credential_closes_with_4401(self):
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/api/v1/realtime/ws"):
                pass

    def test_api_key_query_param_authenticates_and_scopes_to_correct_tenant(self):
        with self.client.websocket_connect("/api/v1/realtime/ws?api_key=secret-a") as ws:
            started = ws.receive_json()
            self.assertEqual(started["kind"], "event")
            self.assertEqual(started["type"], "session.started")
            connected = ws.receive_json()
            self.assertEqual(connected["type"], "session.connected")
            session_id = list(self.bridge._sessions.keys())[0]
            self.assertEqual(self.bridge._sessions[session_id].tenant_id, "tenant-o1")
            self.assertEqual(self.bridge._sessions[session_id].owner_id, "user-a")

    def test_invalid_api_key_closes_with_4401(self):
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/api/v1/realtime/ws?api_key=wrong-key"):
                pass

    def test_session_cookie_authenticates_as_the_logged_in_human_user(self):
        login = self.client.post(
            "/api/accounts/login", json={"username": "owner1", "password": "OwnerPass12!"}
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertIn("panda_session", login.cookies)

        with self.client.websocket_connect("/api/v1/realtime/ws") as ws:
            started = ws.receive_json()
            self.assertEqual(started["type"], "session.started")
            session_id = list(self.bridge._sessions.keys())[0]
            session = self.bridge._sessions[session_id]
            self.assertEqual(session.tenant_id, "tenant-o1")


if __name__ == "__main__":
    unittest.main()
