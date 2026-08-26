"""Moonshot provider unit tests — offline only."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from agents.moonshot_agent import MoonshotAgent, moonshot_enabled, resolve_moonshot_base_url
from agents.moonshot_errors import (
    MOONSHOT_AUTH,
    MOONSHOT_MALFORMED_RESPONSE,
    MOONSHOT_RATE_LIMIT,
    MOONSHOT_TIMEOUT,
    MoonshotProviderError,
)
from agents.moonshot_fake import FakeMoonshotProvider
from agents.moonshot_versions import MOONSHOT_PROVIDER_ID


class _Store:
    def __init__(self, key="sk-test-moonshot-secret"):
        self.key = key

    def get(self, name: str):
        if name == "MOONSHOT_API_KEY":
            return self.key
        return None


class MoonshotProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_by_default(self):
        self.assertFalse(moonshot_enabled({}))

    def test_trusted_base_url_only(self):
        with self.assertRaises(MoonshotProviderError):
            resolve_moonshot_base_url({"MOONSHOT_BASE_URL": "https://evil.example/v1"})

    async def test_fake_success_and_usage(self):
        fake = FakeMoonshotProvider(model="kimi-k3", input_tokens=11, output_tokens=7)
        result = await fake.run("hello")
        self.assertEqual(result.provider_id, MOONSHOT_PROVIDER_ID)
        self.assertEqual(result.input_tokens, 11)
        self.assertEqual(result.output_tokens, 7)

    async def test_fake_timeout_429_auth_malformed(self):
        for mode, cat in (
            ("timeout", MOONSHOT_TIMEOUT),
            ("429", MOONSHOT_RATE_LIMIT),
            ("auth", MOONSHOT_AUTH),
            ("malformed", MOONSHOT_MALFORMED_RESPONSE),
        ):
            fake = FakeMoonshotProvider(mode=mode)
            with self.assertRaises(MoonshotProviderError) as ctx:
                await fake.run("x")
            self.assertEqual(ctx.exception.category, cat)
            if mode == "auth":
                self.assertFalse(ctx.exception.retryable)
            if mode == "429":
                self.assertEqual(fake.retry_count, 1)

    async def test_agent_success_via_transport(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
        transport = MagicMock()
        transport.post = AsyncMock(return_value=response)
        agent = MoonshotAgent(
            secret_store=_Store(),
            env={
                "MOONSHOT_ENABLED": "true",
                "MOONSHOT_DEFAULT_MODEL": "kimi-k3",
                "MOONSHOT_BASE_URL": "https://api.moonshot.ai/v1",
            },
            transport=transport,
        )
        result = await agent.run("prompt")
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.input_tokens, 3)
        self.assertEqual(agent.tool_invocations, 0)
        self.assertNotIn("sk-test", repr(agent))

    async def test_agent_malformed_and_429(self):
        bad = MagicMock()
        bad.status_code = 200
        bad.json.return_value = {"choices": []}
        transport = MagicMock()
        transport.post = AsyncMock(return_value=bad)
        agent = MoonshotAgent(
            secret_store=_Store(),
            env={"MOONSHOT_ENABLED": "true", "MOONSHOT_DEFAULT_MODEL": "kimi-k3"},
            transport=transport,
        )
        with self.assertRaises(MoonshotProviderError) as ctx:
            await agent.run("x")
        self.assertEqual(ctx.exception.category, MOONSHOT_MALFORMED_RESPONSE)

        limited = MagicMock()
        limited.status_code = 429
        transport.post = AsyncMock(return_value=limited)
        with self.assertRaises(MoonshotProviderError) as ctx2:
            await agent.run("x")
        self.assertEqual(ctx2.exception.category, MOONSHOT_RATE_LIMIT)

    async def test_tool_instruction_does_not_invoke_tools(self):
        fake = FakeMoonshotProvider(mode="tool_instruction")
        result = await fake.run("buy now")
        self.assertIn("CALL_TOOL", result.text)
        self.assertEqual(fake.tool_invocations, 0)


if __name__ == "__main__":
    unittest.main()
