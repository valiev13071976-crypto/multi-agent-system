from fastapi.testclient import TestClient
import unittest

from agents.model_profile import InvalidModelProfileError
from agents.model_router import (
    REASON_ALL_AVAILABLE_PROVIDERS,
    REASON_AUTO_CAPABILITY_MATCH,
    REASON_AUTO_GENERAL_FALLBACK,
    REASON_AUTO_PRIORITY_FALLBACK,
    REASON_EXPLICIT_PROVIDER,
)
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
SUPPLIER_TEXT = "найди поставщика"


def load_capability_app(
    *providers,
    auto_order,
    fallback=None,
    **category_env,
):
    overrides = env_for(*providers)
    overrides["AUTO_PROVIDER_ORDER"] = auto_order
    if fallback is not None:
        overrides["AUTO_CAPABILITY_FALLBACK"] = fallback
    overrides.update(category_env)
    return load_app(**overrides)


class CapabilityRoutingTests(unittest.TestCase):

    def _assert_contract(self, payload):
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(payload["role"], "Judge")

    def test_a_auto_technical_selects_anthropic(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
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
                    "role": "technical",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        decision = main_mod.router.last_decision
        self.assertEqual(decision.provider_ids, ("anthropic",))
        self.assertEqual(decision.role_id, "technical")
        self.assertEqual(decision.reason, REASON_AUTO_CAPABILITY_MATCH)
        self.assertEqual(main_mod.router.last_route_context["category"], "technical")
        self._assert_contract(response.json())

    def test_b_auto_strategist_selects_openai(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
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
                    "role": "strategist",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        self.assertEqual(main_mod.router.last_decision.provider_ids, ("openai",))
        self.assertEqual(main_mod.router.last_decision.reason, REASON_AUTO_CAPABILITY_MATCH)
        self._assert_contract(response.json())

    def test_c_role_auto_technical_selects_compatible_provider(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
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
        self.assertEqual(main_mod.router.last_classification.category, "technical")
        self.assertEqual(main_mod.router.last_decision.role_id, "technical")
        self.assertEqual(main_mod.router.last_decision.reason, REASON_AUTO_CAPABILITY_MATCH)
        self._assert_contract(response.json())

    def test_d_role_auto_strategy_selects_compatible_provider(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
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
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        composed = mocks["openai"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("strategist", STRATEGY_TEXT))
        self.assertEqual(main_mod.router.last_classification.category, "strategy")
        self._assert_contract(response.json())

    def test_e_tie_break_uses_auto_provider_order(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
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
                    "role": "technical",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(main_mod.router.last_decision.provider_ids, ("anthropic",))
        self._assert_contract(response.json())

    def test_f_explicit_mode_ignores_capability_filter(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": TECHNICAL_TEXT,
                    "mode": "openai",
                    "role": "technical",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        composed = mocks["openai"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("technical", TECHNICAL_TEXT))
        self.assertEqual(main_mod.router.last_decision.reason, REASON_EXPLICIT_PROVIDER)
        self._assert_contract(response.json())

    def test_g_both_does_not_apply_capability_filter(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": TECHNICAL_TEXT,
                    "mode": "both",
                    "role": "technical",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(
            main_mod.router.last_decision.reason,
            REASON_ALL_AVAILABLE_PROVIDERS,
        )
        self._assert_contract(response.json())

    def test_h_general_fallback_when_no_category_match(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            fallback="general",
            OPENAI_TASK_CATEGORIES="general",
            ANTHROPIC_TASK_CATEGORIES="general",
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
                    "role": "technical",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        self.assertEqual(
            main_mod.router.last_decision.reason,
            REASON_AUTO_GENERAL_FALLBACK,
        )
        self._assert_contract(response.json())

    def test_i_priority_fallback_when_no_category_match(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
            fallback="priority",
            OPENAI_TASK_CATEGORIES="general",
            ANTHROPIC_TASK_CATEGORIES="general",
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
                    "role": "technical",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(
            main_mod.router.last_decision.reason,
            REASON_AUTO_PRIORITY_FALLBACK,
        )
        self._assert_contract(response.json())

    def test_j_error_fallback_returns_structured_503(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            fallback="error",
            OPENAI_TASK_CATEGORIES="general",
            ANTHROPIC_TASK_CATEGORIES="general",
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
                    "role": "technical",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "no_capable_provider")
        self.assertEqual(detail["category"], "technical")
        self.assertEqual(
            detail["message"],
            "No configured provider supports the requested task category.",
        )

    def test_k_unknown_task_category_is_config_error(self):
        with self.assertRaises(InvalidModelProfileError):
            load_capability_app(
                "openai",
                auto_order="openai",
                OPENAI_TASK_CATEGORIES="general,seo",
            )

    def test_l_missing_task_categories_supports_only_general(self):
        main_mod = load_capability_app("openai", auto_order="openai")
        profile = main_mod.router.provider_registry.profile("openai")
        self.assertEqual(profile.task_categories, ("general",))

    def test_m_omitted_mode_is_both(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
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
        self.assertEqual(
            main_mod.router.last_decision.reason,
            REASON_ALL_AVAILABLE_PROVIDERS,
        )
        self._assert_contract(response.json())

    def test_n_omitted_role_is_strategist(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
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
        composed = mocks["openai"].await_args.args[0]
        self.assertEqual(composed, compose_prompt("strategist", TECHNICAL_TEXT))
        self.assertEqual(main_mod.router.last_decision.role_id, "strategist")
        self.assertEqual(main_mod.router.last_route_context["category"], "strategy")
        self.assertIsNone(main_mod.router.last_classification)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        self._assert_contract(response.json())

    def test_o_domain_general_find_supplier(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": SUPPLIER_TEXT,
                    "mode": "auto",
                    "role": "auto",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(main_mod.router.last_classification.category, "general")
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        self.assertEqual(main_mod.router.last_decision.reason, REASON_AUTO_CAPABILITY_MATCH)
        self._assert_contract(response.json())


if __name__ == "__main__":
    unittest.main()
