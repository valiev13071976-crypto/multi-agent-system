import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from security.redaction import REDACTED, redact
from tests.test_mode_routing import env_for
from tests.test_smoke import load_app


class RedactionTests(unittest.TestCase):

    def test_g_redacts_api_key_value(self):
        secret = "sk-live-example-key-value"
        rendered = redact(f"provider failed using {secret}", extra_secrets=(secret,))
        self.assertNotIn(secret, rendered)
        self.assertIn(REDACTED, rendered)

    def test_h_redacts_authorization_bearer(self):
        rendered = redact("Authorization: Bearer abc.def.ghi")
        self.assertNotIn("abc.def.ghi", rendered)
        self.assertIn("Authorization: Bearer", rendered)
        self.assertIn(REDACTED, rendered)

    def test_credential_fields_are_redacted(self):
        rendered = redact('password=hunter2 api_key="abc123"')
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("abc123", rendered)

    def test_ordinary_text_is_kept(self):
        text = "Найди поставщика and compare tokenization"
        self.assertEqual(redact(text), text)

    def test_i_http_error_does_not_include_secret(self):
        secret = "sk-http-leak-secret"
        overrides = env_for("openai")
        overrides["OPENAI_API_KEY"] = secret
        main_mod = load_app(**overrides)

        async def boom(*args, **kwargs):
            raise RuntimeError(f"upstream rejected key {secret}")

        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False):
            with patch.object(main_mod.router, "run", new=boom):
                client = TestClient(main_mod.app)
                response = client.post(
                    "/api/analyze",
                    json={"prompt": "Найди поставщика", "mode": "openai"},
                )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(secret, response.text)


if __name__ == "__main__":
    unittest.main()
