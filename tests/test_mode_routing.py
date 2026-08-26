from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import unittest

from agents.router_v2 import ALLOWED_MODE_VALUES
from tests.test_smoke import CONTRACT_KEYS, load_app


USER_PROMPT = "Найди поставщика"

PROVIDER_ENV = {
    "openai": ("OPENAI_API_KEY", "OPENAI_MODEL"),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL"),
    "grok": ("XAI_API_KEY", "XAI_MODEL"),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"),
    "moonshot": ("MOONSHOT_API_KEY", "MOONSHOT_DEFAULT_MODEL"),
    "mistral": ("MISTRAL_API_KEY", "MISTRAL_DEFAULT_MODEL"),
}


def env_for(*providers):
    overrides = {
        "AUTO_PROVIDER_ORDER": "",
        "MOONSHOT_ENABLED": "false",
        "MISTRAL_ENABLED": "false",
    }
    for provider in providers:
        key_env, model_env = PROVIDER_ENV[provider]
        overrides[key_env] = "fake-key"
        overrides[model_env] = "fake-model"
        if provider == "moonshot":
            overrides["MOONSHOT_ENABLED"] = "true"
        if provider == "mistral":
            overrides["MISTRAL_ENABLED"] = "true"
    return overrides


def mock_provider_runs(manager, *providers):
    mocks = {}
    for provider in providers:
        mocks[provider] = AsyncMock(return_value=f"successful {provider} answer")
    stack = ExitStack()
    for provider, mock_run in mocks.items():
        stack.enter_context(
            patch.object(getattr(manager, provider), "run", new=mock_run)
        )
    return stack, mocks


class ModeRoutingTests(unittest.TestCase):

    def _assert_contract(self, payload):
        for key in CONTRACT_KEYS:
            self.assertIn(key, payload)
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))

    def _analyze(self, *providers, mode, expect_status=200):
        main_mod = load_app(**env_for(*providers))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, *providers)
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": USER_PROMPT, "mode": mode},
            )
        self.assertEqual(response.status_code, expect_status)
        return response, mocks

    def test_mode_openai_calls_only_openai(self):
        response, mocks = self._analyze(
            "openai", "anthropic", mode="openai"
        )
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        self._assert_contract(response.json())

    def test_mode_anthropic_calls_only_anthropic(self):
        response, mocks = self._analyze(
            "openai", "anthropic", mode="anthropic"
        )
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_mode_gemini_calls_only_gemini(self):
        response, mocks = self._analyze(
            "openai", "gemini", mode="gemini"
        )
        self.assertEqual(mocks["gemini"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_mode_grok_calls_only_grok(self):
        response, mocks = self._analyze(
            "openai", "grok", mode="grok"
        )
        self.assertEqual(mocks["grok"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_mode_deepseek_calls_only_deepseek(self):
        response, mocks = self._analyze(
            "openai", "deepseek", mode="deepseek"
        )
        self.assertEqual(mocks["deepseek"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_mode_moonshot_calls_only_moonshot(self):
        response, mocks = self._analyze(
            "openai", "moonshot", mode="moonshot"
        )
        self.assertEqual(mocks["moonshot"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_mode_mistral_calls_only_mistral(self):
        response, mocks = self._analyze(
            "openai", "mistral", mode="mistral"
        )
        self.assertEqual(mocks["mistral"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_mode_both_calls_all_available(self):
        response, mocks = self._analyze(
            "openai", "anthropic", mode="both"
        )
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self._assert_contract(response.json())

    def test_unavailable_selected_provider_does_not_fallback(self):
        response, mocks = self._analyze(
            "openai",
            "anthropic",
            mode="gemini",
            expect_status=503,
        )
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "provider_not_configured")
        self.assertEqual(detail["provider"], "gemini")
        self.assertEqual(detail["mode"], "gemini")

    def test_single_mode_with_no_providers_is_not_generic_unavailable(self):
        main_mod = load_app()
        client = TestClient(main_mod.app)
        response = client.post(
            "/api/analyze",
            json={"prompt": USER_PROMPT, "mode": "openai"},
        )
        self.assertEqual(response.status_code, 503)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "provider_not_configured")
        self.assertNotEqual(detail["error"], "no_providers_available")

    def test_invalid_mode_returns_http_400(self):
        main_mod = load_app(
            OPENAI_API_KEY="fake-key",
            OPENAI_MODEL="fake-model",
        )
        mock_run = AsyncMock(return_value="successful openai answer")
        with patch.object(
            main_mod.router.pipeline.expert_manager.openai,
            "run",
            new=mock_run,
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": USER_PROMPT, "mode": "not-a-provider"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(mock_run.await_count, 0)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "invalid_mode")
        self.assertEqual(detail["mode"], "not-a-provider")

    def test_openapi_keeps_mode_enum(self):
        main_mod = load_app()
        client = TestClient(main_mod.app)
        schema = client.get("/openapi.json").json()
        mode_schema = schema["components"]["schemas"]["AnalyzeRequest"]["properties"]["mode"]
        self.assertEqual(mode_schema.get("enum"), list(ALLOWED_MODE_VALUES))


if __name__ == "__main__":
    unittest.main()
