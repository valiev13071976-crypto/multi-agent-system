from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import unittest

from agents.role_registry import compose_prompt
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


STRATEGY_TEXT = "придумай стратегию продаж"
TECHNICAL_TEXT = """
Traceback (most recent call last):
  File "app.py", line 10, in <module>
    main()
TypeError: 'NoneType' object is not iterable
"""
RESEARCH_TEXT = "найди источники и проверь факты"
TREND_TEXT = "какие сейчас тренды и динамика рынка"


class RoleAutoTests(unittest.TestCase):

    def _assert_contract(self, payload):
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(payload["role"], "Judge")

    def test_openai_auto_strategy_uses_strategist(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": STRATEGY_TEXT,
                    "mode": "openai",
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        composed = mocks["openai"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("strategist", STRATEGY_TEXT))
        self.assertEqual(main_mod.router.last_decision.role_id, "strategist")
        self._assert_contract(response.json())

    def test_anthropic_auto_technical_uses_technical(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": TECHNICAL_TEXT,
                    "mode": "anthropic",
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        composed = mocks["anthropic"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("technical", TECHNICAL_TEXT))
        self.assertEqual(main_mod.router.last_decision.role_id, "technical")
        self._assert_contract(response.json())

    def test_deepseek_auto_research_uses_researcher(self):
        main_mod = load_app(**env_for("openai", "deepseek"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "deepseek")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": RESEARCH_TEXT,
                    "mode": "deepseek",
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["deepseek"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        composed = mocks["deepseek"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("researcher", RESEARCH_TEXT))
        self.assertEqual(main_mod.router.last_decision.role_id, "researcher")
        self._assert_contract(response.json())

    def test_both_auto_trend_sends_identical_trend_prompt(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": TREND_TEXT,
                    "mode": "both",
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        openai_prompt = mocks["openai"].await_args.args[0]
        anthropic_prompt = mocks["anthropic"].await_args.args[0]
        self.assertEqual(openai_prompt, anthropic_prompt)
        self.assertEqual(openai_prompt, compose_prompt("trend_agent", TREND_TEXT))
        self._assert_contract(response.json())

    def test_explicit_critic_wins_over_strategy_text(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with patch.object(
            main_mod.router.task_classifier,
            "classify",
        ) as mock_classify:
            with stack:
                client = TestClient(main_mod.app)
                response = client.post(
                    "/api/analyze",
                    json={
                        "prompt": STRATEGY_TEXT,
                        "mode": "openai",
                        "role": "critic",
                    },
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_classify.call_count, 0)
        composed = mocks["openai"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("critic", STRATEGY_TEXT))
        self._assert_contract(response.json())

    def test_omitted_role_does_not_call_classifier(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with patch.object(
            main_mod.router.task_classifier,
            "classify",
        ) as mock_classify:
            with stack:
                client = TestClient(main_mod.app)
                response = client.post(
                    "/api/analyze",
                    json={
                        "prompt": TECHNICAL_TEXT,
                        "mode": "openai",
                    },
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_classify.call_count, 0)
        composed = mocks["openai"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("strategist", TECHNICAL_TEXT))
        self._assert_contract(response.json())

    def test_auto_find_supplier_falls_back_to_strategist(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": "найди поставщика",
                    "mode": "openai",
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 200)
        composed = mocks["openai"].await_args.args[0]
        self.assertEqual(
            composed,
            compose_prompt("strategist", "найди поставщика"),
        )
        self._assert_contract(response.json())

    def test_auto_seo_audit_falls_back_to_strategist(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": "сделай SEO аудит",
                    "mode": "openai",
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 200)
        composed = mocks["openai"].await_args.args[0]
        self.assertEqual(
            composed,
            compose_prompt("strategist", "сделай SEO аудит"),
        )
        self._assert_contract(response.json())

    def test_invalid_role_returns_400_without_provider_calls(self):
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
                    "prompt": STRATEGY_TEXT,
                    "mode": "openai",
                    "role": "not-a-role",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(mock_run.await_count, 0)
        self.assertEqual(response.json()["detail"]["error"], "invalid_role")

    def test_unavailable_provider_with_auto_does_not_fallback(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": STRATEGY_TEXT,
                    "mode": "gemini",
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "provider_not_configured")
        self.assertEqual(detail["provider"], "gemini")


if __name__ == "__main__":
    unittest.main()
