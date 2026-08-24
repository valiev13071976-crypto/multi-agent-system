from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import unittest

from agents.role_registry import (
    ALLOWED_ROLE_VALUES,
    DEFAULT_ROLE,
    MAX_CONTEXT_CHARS,
    STRATEGIST_PROMPT,
    USER_TASK_MARKER,
    compose_prompt,
)
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


USER_PROMPT = "Найди поставщика"


class RolePromptTests(unittest.TestCase):

    def _assert_contract(self, payload):
        for key in CONTRACT_KEYS:
            self.assertIn(key, payload)
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(payload["role"], "Judge")

    def test_openai_strategist_uses_strategist_instruction(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "openai",
                    "role": "strategist",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        composed = mocks["openai"].await_args.args[0]
        self.assertIn("РОЛЬ: Стратег.", composed)
        self.assertIn(USER_TASK_MARKER, composed)
        self.assertIn(USER_PROMPT, composed)
        self.assertTrue(composed.endswith(USER_PROMPT) or USER_PROMPT in composed)
        self._assert_contract(response.json())

    def test_anthropic_strategist_uses_same_instruction(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "anthropic",
                    "role": "strategist",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        composed = mocks["anthropic"].await_args.args[0]
        openai_composed = compose_prompt("strategist", USER_PROMPT)
        self.assertEqual(composed, openai_composed)
        self.assertIn("РОЛЬ: Стратег.", composed)
        self._assert_contract(response.json())

    def test_deepseek_critic_uses_critic_instruction(self):
        main_mod = load_app(**env_for("openai", "deepseek"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "deepseek")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "deepseek",
                    "role": "critic",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["deepseek"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        composed = mocks["deepseek"].await_args.args[0]
        self.assertIn("РОЛЬ: Критик.", composed)
        self.assertNotIn("РОЛЬ: Стратег.", composed)
        self.assertIn(USER_TASK_MARKER, composed)
        self.assertIn(USER_PROMPT, composed)
        self._assert_contract(response.json())

    def test_both_researcher_sends_identical_prompt(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "both",
                    "role": "researcher",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        openai_prompt = mocks["openai"].await_args.args[0]
        anthropic_prompt = mocks["anthropic"].await_args.args[0]
        self.assertEqual(openai_prompt, anthropic_prompt)
        self.assertIn("РОЛЬ: Исследователь и факт-чекер.", openai_prompt)
        self.assertIn(USER_PROMPT, openai_prompt)
        self._assert_contract(response.json())

    def test_invalid_role_returns_http_400_without_provider_calls(self):
        main_mod = load_app(**env_for("openai"))
        mock_run = AsyncMock(return_value="should not run")
        with patch.object(
            main_mod.router.pipeline.expert_manager.openai,
            "run",
            new=mock_run,
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "openai",
                    "role": "not-a-role",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(mock_run.await_count, 0)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "invalid_role")
        self.assertEqual(detail["role"], "not-a-role")

    def test_trend_agent_uses_canonical_prompt(self):
        main_mod = load_app(**env_for("grok"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "grok")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "grok",
                    "role": "trend_agent",
                },
            )
        self.assertEqual(response.status_code, 200)
        composed = mocks["grok"].await_args.args[0]
        self.assertIn("РОЛЬ: Аналитик трендов.", composed)
        self.assertIn("актуальные тренды", composed)
        self.assertIn("изменения рынка", composed)
        self.assertIn("новые возможности", composed)
        self.assertIn(USER_TASK_MARKER, composed)
        self.assertIn(USER_PROMPT, composed)
        self._assert_contract(response.json())

    def test_composed_prompt_respects_char_limit(self):
        long_request = "x" * (MAX_CONTEXT_CHARS + 5000)
        composed = compose_prompt("strategist", long_request)
        self.assertLessEqual(len(composed), MAX_CONTEXT_CHARS)
        self.assertIn("РОЛЬ: Стратег.", composed)
        self.assertIn(USER_TASK_MARKER, composed)
        self.assertTrue(composed.startswith(STRATEGIST_PROMPT.rstrip()))

    def test_missing_role_defaults_to_strategist(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": USER_PROMPT, "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        composed = mocks["openai"].await_args.args[0]
        self.assertEqual(composed, compose_prompt(DEFAULT_ROLE, USER_PROMPT))
        self._assert_contract(response.json())

    def test_openapi_keeps_role_enum(self):
        main_mod = load_app()
        client = TestClient(main_mod.app)
        schema = client.get("/openapi.json").json()
        role_schema = schema["components"]["schemas"]["AnalyzeRequest"]["properties"]["role"]
        self.assertEqual(role_schema.get("enum"), list(ALLOWED_ROLE_VALUES))
        self.assertEqual(role_schema.get("default"), DEFAULT_ROLE)


if __name__ == "__main__":
    unittest.main()
