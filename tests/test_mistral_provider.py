"""Mistral provider unit tests — offline only."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from agents.mistral_agent import MistralAgent, mistral_enabled, resolve_mistral_base_url
from agents.mistral_errors import (
    MISTRAL_AUTH,
    MISTRAL_MALFORMED_RESPONSE,
    MISTRAL_RATE_LIMIT,
    MISTRAL_TIMEOUT,
    MistralProviderError,
)
from agents.mistral_fake import FakeMistralProvider
from agents.mistral_versions import (
    MISTRAL_PROVIDER_ID,
    resolve_mistral_chat_temperature,
)


class _Store:
    def __init__(self, key="sk-test-mistral-secret"):
        self.key = key

    def get(self, name: str):
        if name == "MISTRAL_API_KEY":
            return self.key
        return None


class MistralProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_by_default(self):
        self.assertFalse(mistral_enabled({}))

    def test_trusted_base_url_only(self):
        with self.assertRaises(MistralProviderError):
            resolve_mistral_base_url({"MISTRAL_BASE_URL": "https://evil.example/v1"})

    async def test_fake_success_and_usage(self):
        fake = FakeMistralProvider(model="mistral-large-latest", input_tokens=11, output_tokens=7)
        result = await fake.run("hello")
        self.assertEqual(result.provider_id, MISTRAL_PROVIDER_ID)
        self.assertEqual(result.input_tokens, 11)
        self.assertEqual(result.output_tokens, 7)

    async def test_fake_timeout_429_auth_malformed(self):
        for mode, cat in (
            ("timeout", MISTRAL_TIMEOUT),
            ("429", MISTRAL_RATE_LIMIT),
            ("auth", MISTRAL_AUTH),
            ("malformed", MISTRAL_MALFORMED_RESPONSE),
        ):
            fake = FakeMistralProvider(mode=mode)
            with self.assertRaises(MistralProviderError) as ctx:
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
        agent = MistralAgent(
            secret_store=_Store(),
            env={
                "MISTRAL_ENABLED": "true",
                "MISTRAL_DEFAULT_MODEL": "mistral-large-latest",
                "MISTRAL_BASE_URL": "https://api.mistral.ai/v1",
            },
            transport=transport,
        )
        result = await agent.run("prompt")
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.input_tokens, 3)
        self.assertEqual(agent.tool_invocations, 0)
        self.assertNotIn("sk-test", repr(agent))
        kwargs = transport.post.await_args.kwargs
        body = kwargs.get("json") or {}
        self.assertEqual(body.get("model"), "mistral-large-latest")
        self.assertNotIn("temperature", body)
        self.assertEqual(sorted(body.keys()), ["messages", "model"])
        self.assertEqual(
            body.get("messages"),
            [{"role": "user", "content": "prompt"}],
        )
        headers = kwargs.get("headers") or {}
        self.assertIn("Authorization", headers)
        self.assertTrue(str(headers["Authorization"]).startswith("Bearer "))

    async def test_default_request_omits_temperature_for_large_and_small(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "MISTRAL_OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        for model_id in ("mistral-large-latest", "mistral-small-latest"):
            transport = MagicMock()
            transport.post = AsyncMock(return_value=response)
            agent = MistralAgent(
                secret_store=_Store(),
                env={
                    "MISTRAL_ENABLED": "true",
                    "MISTRAL_DEFAULT_MODEL": model_id,
                    "MISTRAL_BASE_URL": "https://api.mistral.ai/v1",
                },
                transport=transport,
            )
            await agent.run("Reply exactly: MISTRAL_OK")
            body = transport.post.await_args.kwargs.get("json") or {}
            self.assertEqual(body.get("model"), model_id)
            self.assertNotIn("temperature", body)
            self.assertEqual(set(body.keys()), {"model", "messages"})

    async def test_explicit_temperature_included_when_configured(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        transport = MagicMock()
        transport.post = AsyncMock(return_value=response)
        agent = MistralAgent(
            secret_store=_Store(),
            env={
                "MISTRAL_ENABLED": "true",
                "MISTRAL_DEFAULT_MODEL": "mistral-large-latest",
                "MISTRAL_TEMPERATURE": "0.2",
            },
            transport=transport,
        )
        await agent.run("x")
        body = transport.post.await_args.kwargs.get("json") or {}
        self.assertEqual(body.get("temperature"), 0.2)

    async def test_agent_malformed_and_429(self):
        bad = MagicMock()
        bad.status_code = 200
        bad.json.return_value = {"choices": []}
        transport = MagicMock()
        transport.post = AsyncMock(return_value=bad)
        agent = MistralAgent(
            secret_store=_Store(),
            env={"MISTRAL_ENABLED": "true", "MISTRAL_DEFAULT_MODEL": "mistral-large-latest"},
            transport=transport,
        )
        with self.assertRaises(MistralProviderError) as ctx:
            await agent.run("x")
        self.assertEqual(ctx.exception.category, MISTRAL_MALFORMED_RESPONSE)

        limited = MagicMock()
        limited.status_code = 429
        transport.post = AsyncMock(return_value=limited)
        with self.assertRaises(MistralProviderError) as ctx2:
            await agent.run("x")
        self.assertEqual(ctx2.exception.category, MISTRAL_RATE_LIMIT)

    async def test_agent_auth_error(self):
        response = MagicMock()
        response.status_code = 401
        transport = MagicMock()
        transport.post = AsyncMock(return_value=response)
        agent = MistralAgent(
            secret_store=_Store(),
            env={"MISTRAL_ENABLED": "true", "MISTRAL_DEFAULT_MODEL": "mistral-large-latest"},
            transport=transport,
        )
        with self.assertRaises(MistralProviderError) as ctx:
            await agent.run("x")
        self.assertEqual(ctx.exception.category, MISTRAL_AUTH)
        self.assertFalse(ctx.exception.retryable)

    async def test_missing_key_and_model(self):
        with self.assertRaises(MistralProviderError) as ctx:
            MistralAgent(
                secret_store=_Store(key=""),
                env={"MISTRAL_ENABLED": "true", "MISTRAL_DEFAULT_MODEL": "mistral-large-latest"},
            )
        self.assertEqual(ctx.exception.category, "missing_key")
        with self.assertRaises(MistralProviderError) as ctx2:
            MistralAgent(
                secret_store=_Store(),
                env={"MISTRAL_ENABLED": "true", "MISTRAL_DEFAULT_MODEL": ""},
            )
        self.assertEqual(ctx2.exception.category, "missing_model")

    async def test_tool_instruction_does_not_invoke_tools(self):
        fake = FakeMistralProvider(mode="tool_instruction")
        result = await fake.run("buy now")
        self.assertIn("CALL_TOOL", result.text)
        self.assertEqual(fake.tool_invocations, 0)

    def test_resolve_temperature_omitted_by_default(self):
        self.assertIsNone(resolve_mistral_chat_temperature({}))
        self.assertIsNone(resolve_mistral_chat_temperature({"MISTRAL_TEMPERATURE": ""}))
        self.assertEqual(resolve_mistral_chat_temperature({"MISTRAL_TEMPERATURE": "0.2"}), 0.2)
        self.assertIsNone(resolve_mistral_chat_temperature({"MISTRAL_TEMPERATURE": "bad"}))


if __name__ == "__main__":
    unittest.main()
