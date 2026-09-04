"""Focused tests for /login POST wiring and /app panda_session recognition."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from accounts.dual_auth import configure_accounts_auth, install_dual_auth
from accounts.models import ROLE_OWNER
from accounts.router import configure_accounts_router
from accounts.runtime import build_accounts_runtime
from business_assistant_api.router import configure_business_assistant_api_router
from business_assistant_api.runtime import build_business_assistant_api_runtime
from security.api_auth import configure_security
from security.auth import AuthService


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


class LoginFormStaticWiringTests(unittest.TestCase):
    def test_login_form_posts_to_accounts_api_not_get(self):
        html = _read("static/accounts/login.html")
        self.assertRegex(html, r'<form[^>]*id="login-form"')
        self.assertIn('method="post"', html)
        self.assertIn('action="/api/accounts/login"', html)

    def test_login_js_sends_json_post_and_redirects_to_app(self):
        js = _read("static/accounts/login.js")
        self.assertIn("preventDefault", js)
        self.assertIn('fetch("/api/accounts/login"', js)
        self.assertIn('method: "POST"', js)
        self.assertIn("application/json", js)
        self.assertIn('credentials: "same-origin"', js)
        self.assertIn('window.location.href = "/app"', js)
        self.assertNotIn("console.log", js)
        self.assertNotIn("console.debug", js)
        self.assertNotIn("console.info", js)

    def test_app_boot_recognizes_human_session_and_keeps_api_key(self):
        app_js = _read("static/panda/js/app.js")
        client = _read("static/panda/js/api-client.js")
        self.assertIn("hasHumanSession", app_js)
        self.assertIn("hasHumanSession", client)
        self.assertIn("/api/accounts/me", client)
        self.assertIn("auth_method === \"session\"", client)
        self.assertIn("hasApiKey", app_js)
        self.assertIn("panda_api_key", client)
        self.assertIn("/api/accounts/logout", app_js)
        self.assertIn("X-CSRF-Token", app_js)
        role = _read("static/shared/role-context.js")
        self.assertIn("/api/accounts/me", role)
        self.assertIn("auth_method === \"session\"", role)
        html = _read("static/panda/index.html")
        self.assertIn('href="/login"', html)
        self.assertIn('id="auth-gate"', html)


class SessionCookieBaApiWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.accounts_db = os.path.join(self.tmp, "accounts.sqlite")
        self.ba_db = os.path.join(self.tmp, "ba.sqlite")
        self.runtime = build_accounts_runtime(env={"ACCOUNTS_DB_PATH": self.accounts_db})
        self.ba = build_business_assistant_api_runtime(db_path=self.ba_db)
        configure_security(
            auth=AuthService(
                env={
                    "SECURITY_AUTH_MODE": "required",
                    "PANDA_API_KEYS": "key-a|tenant-o1|user-a|user|secret-a",
                }
            )
        )
        configure_accounts_auth(self.runtime.service)
        install_dual_auth()
        app = FastAPI()
        app.include_router(configure_accounts_router(self.runtime.service))
        app.include_router(configure_business_assistant_api_router(self.ba.service))
        self.client = TestClient(app)
        self.runtime.service.identity.create_user(
            username="owner1",
            password="OwnerPass12!",
            tenant_id="tenant-o1",
            role=ROLE_OWNER,
            actor_id="bootstrap",
            is_bootstrap_owner=True,
            protected=True,
        )

    def tearDown(self):
        self.runtime.close()
        self.ba.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_json_login_then_ba_conversations_without_api_key(self):
        login = self.client.post(
            "/api/accounts/login",
            json={"username": "owner1", "password": "OwnerPass12!"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertIn("panda_session", login.cookies)
        me = self.client.get("/api/accounts/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json().get("auth_method"), "session")
        listed = self.client.get("/api/v1/business-assistant/conversations")
        self.assertEqual(listed.status_code, 200, listed.text)
        created = self.client.post(
            "/api/v1/business-assistant/conversations",
            json={"title": "Owner chat"},
        )
        self.assertEqual(created.status_code, 200, created.text)

    def test_api_key_still_lists_conversations(self):
        r = self.client.get(
            "/api/v1/business-assistant/conversations",
            headers={"X-API-Key": "secret-a"},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_unauthenticated_ba_still_denied(self):
        r = self.client.get("/api/v1/business-assistant/conversations")
        self.assertIn(r.status_code, {401, 403})


if __name__ == "__main__":
    unittest.main()
