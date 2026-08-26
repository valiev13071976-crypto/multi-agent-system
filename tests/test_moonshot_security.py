"""Moonshot security — no key leakage."""

from __future__ import annotations

import unittest

from agents.moonshot_agent import MoonshotAgent
from agents.moonshot_errors import MoonshotProviderError
from security.redaction import redact
from security.secrets import SECRET_ENV_NAMES, SecretProvider


class _Store:
    def __init__(self, key="sk-moonshot-LEAKME"):
        self.key = key

    def get(self, name: str):
        if name == "MOONSHOT_API_KEY":
            return self.key
        return None


class MoonshotSecurityTests(unittest.TestCase):
    def test_secret_env_registered(self):
        self.assertIn("MOONSHOT_API_KEY", SECRET_ENV_NAMES)

    def test_repr_and_error_have_no_key(self):
        agent = MoonshotAgent(
            secret_store=_Store(),
            env={"MOONSHOT_ENABLED": "true", "MOONSHOT_DEFAULT_MODEL": "kimi-k3"},
        )
        blob = repr(agent)
        self.assertNotIn("LEAKME", blob)
        self.assertNotIn("sk-moonshot", blob)
        err = MoonshotProviderError("auth", status_code=401)
        self.assertNotIn("LEAKME", repr(err))
        self.assertNotIn("LEAKME", str(err))

    def test_redaction_via_secret_provider(self):
        provider = SecretProvider(store=_Store())
        values = provider.known_secret_values()
        self.assertTrue(any("LEAKME" in v for v in values))
        cleaned = redact("token=sk-moonshot-LEAKME", extra_secrets=values)
        self.assertNotIn("LEAKME", cleaned)


if __name__ == "__main__":
    unittest.main()
