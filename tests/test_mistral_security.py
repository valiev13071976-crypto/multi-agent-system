"""Mistral security — no key leakage."""

from __future__ import annotations

import unittest

from agents.mistral_agent import MistralAgent
from agents.mistral_errors import MistralProviderError
from security.redaction import redact
from security.secrets import SECRET_ENV_NAMES, SecretProvider


class _Store:
    def __init__(self, key="sk-mistral-LEAKME"):
        self.key = key

    def get(self, name: str):
        if name == "MISTRAL_API_KEY":
            return self.key
        return None


class MistralSecurityTests(unittest.TestCase):
    def test_secret_env_registered(self):
        self.assertIn("MISTRAL_API_KEY", SECRET_ENV_NAMES)

    def test_repr_and_error_have_no_key(self):
        agent = MistralAgent(
            secret_store=_Store(),
            env={"MISTRAL_ENABLED": "true", "MISTRAL_DEFAULT_MODEL": "mistral-large-latest"},
        )
        blob = repr(agent)
        self.assertNotIn("LEAKME", blob)
        self.assertNotIn("sk-mistral", blob)
        err = MistralProviderError("auth", status_code=401)
        self.assertNotIn("LEAKME", repr(err))
        self.assertNotIn("LEAKME", str(err))

    def test_redaction_via_secret_provider(self):
        provider = SecretProvider(store=_Store())
        values = provider.known_secret_values()
        self.assertTrue(any("LEAKME" in v for v in values))
        cleaned = redact("token=sk-mistral-LEAKME", extra_secrets=values)
        self.assertNotIn("LEAKME", cleaned)


if __name__ == "__main__":
    unittest.main()
