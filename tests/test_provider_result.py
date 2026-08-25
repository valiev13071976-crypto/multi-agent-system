from dataclasses import FrozenInstanceError
import os
import unittest
from unittest.mock import patch

import httpx

from agents.anthropic_agent import AnthropicAgent
from agents.deepseek_agent import DeepSeekAgent
from agents.gemini_agent import GeminiAgent
from agents.grok_agent import GrokAgent
from agents.openai_agent import OpenAIAgent
from agents.provider_result import (
    ProviderResult,
    usage_from_anthropic_response,
    usage_from_chat_completions_response,
    usage_from_gemini_response,
    usage_from_openai_response,
)


SECRET = "sk-provider-result-secret"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "should-not-be-copied"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("POST", "https://example.test"),
                response=httpx.Response(self.status_code),
            )


class FakeAsyncClient:
    def __init__(self, response, *args, **kwargs):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return self._response


def _client_factory(payload, status_code=200):
    response = FakeResponse(payload, status_code=status_code)

    def factory(*args, **kwargs):
        return FakeAsyncClient(response, *args, **kwargs)

    return factory


class ProviderResultTests(unittest.IsolatedAsyncioTestCase):

    def test_provider_result_is_immutable(self):
        result = ProviderResult(
            text="hello",
            provider_id="openai",
            model_id="gpt-test",
            input_tokens=1,
            output_tokens=2,
            total_tokens=3,
        )
        with self.assertRaises(FrozenInstanceError):
            result.text = "nope"

    def test_a_openai_usage_fields(self):
        data = {
            "output_text": "ok",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "api_key": SECRET,
            },
        }
        result = usage_from_openai_response(
            data, provider_id="openai", model_id="gpt-test", text="ok"
        )
        self.assertEqual(result.input_tokens, 11)
        self.assertEqual(result.output_tokens, 7)
        self.assertEqual(result.total_tokens, 18)
        self.assertNotIn("api_key", result.raw_usage)
        self.assertNotIn(SECRET, repr(result))

    def test_a_openai_chat_style_usage_aliases(self):
        result = usage_from_openai_response(
            {"usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}},
            provider_id="openai",
            model_id="m",
            text="t",
        )
        self.assertEqual(result.input_tokens, 4)
        self.assertEqual(result.output_tokens, 6)
        self.assertEqual(result.total_tokens, 10)

    def test_a_anthropic_usage_fields(self):
        result = usage_from_anthropic_response(
            {"usage": {"input_tokens": 21, "output_tokens": 3}},
            provider_id="anthropic",
            model_id="claude",
            text="t",
        )
        self.assertEqual(result.input_tokens, 21)
        self.assertEqual(result.output_tokens, 3)
        self.assertEqual(result.total_tokens, 24)

    def test_a_gemini_usage_fields(self):
        result = usage_from_gemini_response(
            {
                "usageMetadata": {
                    "promptTokenCount": 9,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 14,
                }
            },
            provider_id="gemini",
            model_id="gemini",
            text="t",
        )
        self.assertEqual(result.input_tokens, 9)
        self.assertEqual(result.output_tokens, 5)
        self.assertEqual(result.total_tokens, 14)

    def test_a_chat_completions_usage_fields(self):
        result = usage_from_chat_completions_response(
            {"usage": {"prompt_tokens": 2, "completion_tokens": 8, "total_tokens": 10}},
            provider_id="grok",
            model_id="grok",
            text="t",
        )
        self.assertEqual(result.input_tokens, 2)
        self.assertEqual(result.output_tokens, 8)
        self.assertEqual(result.total_tokens, 10)

    def test_b_missing_usage_is_none(self):
        openai = usage_from_openai_response(
            {"output_text": "ok"}, provider_id="openai", model_id="m", text="ok"
        )
        anthropic = usage_from_anthropic_response(
            {}, provider_id="anthropic", model_id="m", text="ok"
        )
        gemini = usage_from_gemini_response(
            {}, provider_id="gemini", model_id="m", text="ok"
        )
        chat = usage_from_chat_completions_response(
            {}, provider_id="deepseek", model_id="m", text="ok"
        )
        for result in (openai, anthropic, gemini, chat):
            self.assertIsNone(result.input_tokens)
            self.assertIsNone(result.output_tokens)
            self.assertIsNone(result.total_tokens)

    def test_b_malformed_usage_is_none(self):
        result = usage_from_openai_response(
            {"usage": "not-a-dict"},
            provider_id="openai",
            model_id="m",
            text="ok",
        )
        self.assertIsNone(result.input_tokens)

    async def test_a_openai_client_extracts_usage(self):
        payload = {
            "output_text": "hello",
            "usage": {"input_tokens": 15, "output_tokens": 4, "total_tokens": 19},
        }
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": SECRET, "OPENAI_MODEL": "gpt-test"},
            clear=False,
        ), patch("httpx.AsyncClient", _client_factory(payload)):
            result = await OpenAIAgent().run("prompt")
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.input_tokens, 15)
        self.assertEqual(result.output_tokens, 4)
        self.assertEqual(result.total_tokens, 19)
        self.assertNotIn(SECRET, repr(result))

    async def test_b_openai_client_missing_usage(self):
        payload = {"output_text": "hello"}
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": SECRET, "OPENAI_MODEL": "gpt-test"},
            clear=False,
        ), patch("httpx.AsyncClient", _client_factory(payload)):
            result = await OpenAIAgent().run("prompt")
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        self.assertIsNone(result.total_tokens)

    async def test_a_anthropic_client_extracts_usage(self):
        payload = {
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 30, "output_tokens": 2},
        }
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": SECRET, "ANTHROPIC_MODEL": "claude"},
            clear=False,
        ), patch("httpx.AsyncClient", _client_factory(payload)):
            result = await AnthropicAgent().run("prompt")
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.input_tokens, 30)
        self.assertEqual(result.output_tokens, 2)

    async def test_a_gemini_client_extracts_usage(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 3,
                "totalTokenCount": 15,
            },
        }
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SECRET, "GEMINI_MODEL": "gemini"},
            clear=False,
        ), patch("httpx.AsyncClient", _client_factory(payload)):
            result = await GeminiAgent().run("prompt")
        self.assertEqual(result.input_tokens, 12)
        self.assertEqual(result.output_tokens, 3)
        self.assertEqual(result.total_tokens, 15)

    async def test_a_grok_client_extracts_usage(self):
        payload = {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
        }
        with patch.dict(
            os.environ,
            {"XAI_API_KEY": SECRET, "XAI_MODEL": "grok"},
            clear=False,
        ), patch("httpx.AsyncClient", _client_factory(payload)):
            result = await GrokAgent().run("prompt")
        self.assertEqual(result.input_tokens, 8)
        self.assertEqual(result.output_tokens, 1)

    async def test_a_deepseek_client_extracts_usage(self):
        payload = {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
        }
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": SECRET, "DEEPSEEK_MODEL": "deepseek"},
            clear=False,
        ), patch("httpx.AsyncClient", _client_factory(payload)):
            result = await DeepSeekAgent().run("prompt")
        self.assertEqual(result.input_tokens, 5)
        self.assertEqual(result.output_tokens, 6)
        self.assertEqual(result.total_tokens, 11)


if __name__ == "__main__":
    unittest.main()
