import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agents.context_manager import ContextManager
from tests.test_smoke import CONTRACT_KEYS, load_app


USER_PROMPT = "Найди поставщика"


class ContextTests(unittest.TestCase):

    def test_prepare_returns_clean_user_request_str(self):
        manager = ContextManager()
        self.assertEqual(manager.max_chars, 30000)

        result = asyncio.run(
            manager.prepare(USER_PROMPT, extra_context={"foo": "bar"})
        )

        self.assertIsInstance(result, str)
        self.assertEqual(result, USER_PROMPT)
        self.assertNotIn("{'context':", result)
        self.assertNotIn("{'user_request':", result)
        self.assertNotIn('"user_request"', result)

    def test_prepare_truncates_to_max_chars(self):
        manager = ContextManager()
        oversized = "x" * (manager.max_chars + 5000)

        result = asyncio.run(manager.prepare(oversized))

        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 30000)
        self.assertEqual(result, oversized[:30000])
        self.assertNotIn("{'context':", result)

    def test_original_prompt_reaches_agent_run_without_dict_repr(self):
        main_mod = load_app(
            OPENAI_API_KEY="fake-key",
            OPENAI_MODEL="fake-model",
        )
        captured = []

        async def fake_run(prompt):
            captured.append(prompt)
            return "successful strategist answer"

        with patch.object(
            main_mod.router.pipeline.expert_manager.openai,
            "run",
            new=fake_run,
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": USER_PROMPT, "mode": "openai"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        self.assertIn("USER TASK:", captured[0])
        self.assertIn(USER_PROMPT, captured[0])
        self.assertIn("РОЛЬ: Стратег.", captured[0])
        self.assertNotIn("{'context':", captured[0])
        self.assertNotIn("{'user_request':", captured[0])
        self.assertNotIn("'cleaned':", captured[0])

        payload = response.json()
        for key in CONTRACT_KEYS:
            self.assertIn(key, payload)
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))

    def test_provider_call_count_matches_available_providers(self):
        main_mod = load_app(
            OPENAI_API_KEY="fake-key",
            OPENAI_MODEL="fake-model",
        )
        mock_run = AsyncMock(return_value="successful strategist answer")

        with patch.object(
            main_mod.router.pipeline.expert_manager.openai,
            "run",
            new=mock_run,
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": USER_PROMPT, "mode": "both"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_run.await_count, 1)
        self.assertEqual(mock_run.await_args.args[0].count("USER TASK:"), 1)
        self.assertIn(USER_PROMPT, mock_run.await_args.args[0])

    def test_mode_openai_runs_only_openai(self):
        main_mod = load_app(
            OPENAI_API_KEY="fake-key",
            OPENAI_MODEL="fake-model",
            ANTHROPIC_API_KEY="fake-key",
            ANTHROPIC_MODEL="fake-model",
        )
        manager = main_mod.router.pipeline.expert_manager
        openai_run = AsyncMock(return_value="successful strategist answer")
        anthropic_run = AsyncMock(return_value="successful critic answer")

        with patch.object(manager.openai, "run", new=openai_run), patch.object(
            manager.anthropic,
            "run",
            new=anthropic_run,
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": USER_PROMPT, "mode": "openai"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(openai_run.await_count, 1)
        self.assertEqual(anthropic_run.await_count, 0)
        self.assertIn(USER_PROMPT, openai_run.await_args.args[0])
        self.assertIn("USER TASK:", openai_run.await_args.args[0])

        payload = response.json()
        for key in CONTRACT_KEYS:
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
