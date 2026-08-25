from fastapi.testclient import TestClient
import unittest

from agents.model_router import REASON_AUTO_PROVIDER, REASON_EXPLICIT_PROVIDER, ModelRouter
from agents.provider_registry import (
    PROVIDER_IDS,
    InvalidAutoProviderOrderError,
    ProviderRegistry,
    parse_auto_provider_order,
)
from agents.role_registry import compose_prompt
from tests.test_model_router import registry_with
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


TECHNICAL_TEXT = """
Traceback (most recent call last):
  File "app.py", line 10, in <module>
    main()
TypeError: 'NoneType' object is not iterable
"""
STRATEGY_TEXT = "придумай стратегию продаж"


def load_auto_app(*providers, auto_order):
    overrides = env_for(*providers)
    overrides["AUTO_PROVIDER_ORDER"] = auto_order
    return load_app(**overrides)


class AutoProviderOrderParseTests(unittest.TestCase):

    def test_empty_uses_provider_ids_default(self):
        self.assertEqual(parse_auto_provider_order(None), PROVIDER_IDS)
        self.assertEqual(parse_auto_provider_order(""), PROVIDER_IDS)
        self.assertEqual(parse_auto_provider_order("   "), PROVIDER_IDS)

    def test_duplicates_are_normalized_first_wins(self):
        self.assertEqual(
            parse_auto_provider_order("openai,openai,anthropic"),
            ("openai", "anthropic"),
        )

    def test_unknown_provider_raises_configuration_error(self):
        with self.assertRaises(InvalidAutoProviderOrderError) as ctx:
            parse_auto_provider_order("openai,foo,anthropic")
        self.assertIn("foo", ctx.exception.unknown)
        self.assertIn("foo", str(ctx.exception))

    def test_whitespace_is_trimmed(self):
        self.assertEqual(
            parse_auto_provider_order(" anthropic , openai "),
            ("anthropic", "openai"),
        )


class AutoModelRouterTests(unittest.TestCase):

    def test_auto_picks_first_available_in_order(self):
        registry = registry_with("openai", "anthropic")
        registry.auto_provider_order = ("anthropic", "openai")
        decision = ModelRouter(registry).decide(mode="auto", role_id="technical")
        self.assertEqual(decision.role_id, "technical")
        self.assertEqual(decision.provider_ids, ("anthropic",))
        self.assertEqual(decision.reason, REASON_AUTO_PROVIDER)
        self.assertEqual(list(decision.models.keys()), ["anthropic"])

    def test_auto_skips_unavailable_and_picks_next(self):
        registry = registry_with("openai")
        registry.auto_provider_order = ("anthropic", "openai")
        decision = ModelRouter(registry).decide(mode="auto", role_id="strategist")
        self.assertEqual(decision.provider_ids, ("openai",))
        self.assertEqual(decision.reason, REASON_AUTO_PROVIDER)

    def test_auto_with_no_available_returns_empty_providers(self):
        registry = registry_with()
        registry.auto_provider_order = ("anthropic", "openai")
        decision = ModelRouter(registry).decide(mode="auto", role_id="strategist")
        self.assertEqual(decision.provider_ids, ())
        self.assertEqual(decision.reason, REASON_AUTO_PROVIDER)


class ModeAutoHttpTests(unittest.TestCase):

    def _assert_contract(self, payload):
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(payload["role"], "Judge")

    def test_a_auto_order_picks_only_anthropic(self):
        main_mod = load_auto_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "auto"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(main_mod.router.last_decision.provider_ids, ("anthropic",))
        self.assertEqual(main_mod.router.last_decision.reason, REASON_AUTO_PROVIDER)
        self._assert_contract(response.json())

    def test_b_auto_skips_unavailable_anthropic(self):
        main_mod = load_auto_app("openai", auto_order="anthropic,openai")
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "auto"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(main_mod.router.last_decision.provider_ids, ("openai",))
        self._assert_contract(response.json())

    def test_c_auto_both_unavailable_is_no_providers(self):
        main_mod = load_auto_app(auto_order="anthropic,openai")
        client = TestClient(main_mod.app)
        response = client.post(
            "/api/analyze",
            json={"prompt": STRATEGY_TEXT, "mode": "auto"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["error"], "no_providers_available")

    def test_d_explicit_openai_unavailable_does_not_fallback(self):
        main_mod = load_auto_app(
            "anthropic",
            auto_order="anthropic,openai",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "openai"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "provider_not_configured")
        self.assertEqual(detail["provider"], "openai")

    def test_e_mode_both_still_calls_all_available(self):
        main_mod = load_auto_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "both"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self._assert_contract(response.json())

    def test_f_auto_mode_keeps_explicit_critic_role(self):
        main_mod = load_auto_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": STRATEGY_TEXT,
                    "mode": "auto",
                    "role": "critic",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        composed = mocks["anthropic"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("critic", STRATEGY_TEXT))
        self.assertEqual(main_mod.router.last_decision.role_id, "critic")
        self._assert_contract(response.json())

    def test_g_auto_mode_and_auto_role_technical(self):
        main_mod = load_auto_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": TECHNICAL_TEXT,
                    "mode": "auto",
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        composed = mocks["anthropic"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("technical", TECHNICAL_TEXT))
        decision = main_mod.router.last_decision
        self.assertEqual(decision.role_id, "technical")
        self.assertEqual(decision.provider_ids, ("anthropic",))
        self.assertEqual(decision.reason, REASON_AUTO_PROVIDER)
        self._assert_contract(response.json())

    def test_h_explicit_anthropic_ignores_provider_auto(self):
        main_mod = load_auto_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
        )
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
        decision = main_mod.router.last_decision
        self.assertEqual(decision.provider_ids, ("anthropic",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)
        self._assert_contract(response.json())

    def test_i_invalid_auto_order_is_configuration_error_at_init(self):
        with self.assertRaises(InvalidAutoProviderOrderError) as ctx:
            load_auto_app("openai", auto_order="openai,foo,anthropic")
        self.assertIn("foo", ctx.exception.unknown)

    def test_j_from_env_normalizes_duplicates(self):
        import os
        from unittest.mock import patch

        env = env_for("openai", "anthropic")
        env["AUTO_PROVIDER_ORDER"] = "openai,openai,anthropic"
        with patch.dict(os.environ, env, clear=False):
            registry = ProviderRegistry.from_env()
        self.assertEqual(registry.auto_provider_order, ("openai", "anthropic"))

    def test_k_omitted_mode_is_both_not_auto(self):
        main_mod = load_auto_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertNotEqual(
            main_mod.router.last_decision.reason,
            REASON_AUTO_PROVIDER,
        )
        self._assert_contract(response.json())

    def test_l_omitted_role_is_strategist_not_auto(self):
        main_mod = load_auto_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": TECHNICAL_TEXT, "mode": "auto"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        composed = mocks["anthropic"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("strategist", TECHNICAL_TEXT))
        self.assertEqual(main_mod.router.last_decision.role_id, "strategist")
        self.assertIsNone(main_mod.router.last_classification)
        self._assert_contract(response.json())


if __name__ == "__main__":
    unittest.main()
