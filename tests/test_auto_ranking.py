from fastapi.testclient import TestClient
import unittest

from agents.model_profile import InvalidAutoRoutingPolicyError
from agents.model_router import (
    REASON_ALL_AVAILABLE_PROVIDERS,
    REASON_AUTO_CAPABILITY_MATCH,
    REASON_AUTO_GENERAL_FALLBACK,
    REASON_AUTO_PRIORITY_FALLBACK,
    REASON_EXPLICIT_PROVIDER,
)
from tests.test_capability_routing import TECHNICAL_TEXT, load_capability_app
from tests.test_mode_routing import mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS


class AutoRankingTests(unittest.TestCase):

    def _assert_contract(self, payload):
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(payload["role"], "Judge")

    def _analyze_auto(self, main_mod, *providers, role="technical"):
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, *providers)
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": TECHNICAL_TEXT,
                    "mode": "auto",
                    "role": role,
                },
            )
        return response, mocks

    def test_a_priority_uses_auto_provider_order(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
            AUTO_ROUTING_POLICY="priority",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(main_mod.router.last_decision.reason, REASON_AUTO_CAPABILITY_MATCH)
        self.assertEqual(main_mod.router.last_route_context["policy"], "priority")
        self._assert_contract(response.json())

    def test_b_quality_prefers_premium(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            AUTO_ROUTING_POLICY="quality",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_QUALITY_CLASS="standard",
            ANTHROPIC_QUALITY_CLASS="premium",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_c_quality_tie_uses_order(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            AUTO_ROUTING_POLICY="quality",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_QUALITY_CLASS="premium",
            ANTHROPIC_QUALITY_CLASS="premium",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        self._assert_contract(response.json())

    def test_d_cost_prefers_cheap(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            AUTO_ROUTING_POLICY="cost",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_COST_CLASS="premium",
            ANTHROPIC_COST_CLASS="cheap",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_e_latency_prefers_fast(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            AUTO_ROUTING_POLICY="latency",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_LATENCY_CLASS="slow",
            ANTHROPIC_LATENCY_CLASS="fast",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_f_balanced_prefers_higher_score(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            AUTO_ROUTING_POLICY="balanced",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_QUALITY_CLASS="premium",
            OPENAI_COST_CLASS="premium",
            OPENAI_LATENCY_CLASS="slow",
            ANTHROPIC_QUALITY_CLASS="standard",
            ANTHROPIC_COST_CLASS="cheap",
            ANTHROPIC_LATENCY_CLASS="fast",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self._assert_contract(response.json())

    def test_g_balanced_tie_uses_order(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            AUTO_ROUTING_POLICY="balanced",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_QUALITY_CLASS="standard",
            OPENAI_COST_CLASS="standard",
            OPENAI_LATENCY_CLASS="standard",
            ANTHROPIC_QUALITY_CLASS="standard",
            ANTHROPIC_COST_CLASS="standard",
            ANTHROPIC_LATENCY_CLASS="standard",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        self._assert_contract(response.json())

    def test_h_capability_wins_over_ranking(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            AUTO_ROUTING_POLICY="quality",
            OPENAI_TASK_CATEGORIES="general,strategy",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_QUALITY_CLASS="premium",
            OPENAI_COST_CLASS="cheap",
            OPENAI_LATENCY_CLASS="fast",
            ANTHROPIC_QUALITY_CLASS="standard",
            ANTHROPIC_COST_CLASS="standard",
            ANTHROPIC_LATENCY_CLASS="standard",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(main_mod.router.last_decision.reason, REASON_AUTO_CAPABILITY_MATCH)
        self._assert_contract(response.json())

    def test_i_general_fallback_applies_quality_policy(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            fallback="general",
            AUTO_ROUTING_POLICY="quality",
            OPENAI_TASK_CATEGORIES="general",
            ANTHROPIC_TASK_CATEGORIES="general",
            OPENAI_QUALITY_CLASS="standard",
            ANTHROPIC_QUALITY_CLASS="premium",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(main_mod.router.last_decision.reason, REASON_AUTO_GENERAL_FALLBACK)
        self._assert_contract(response.json())

    def test_j_priority_fallback_ignores_quality_policy(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
            fallback="priority",
            AUTO_ROUTING_POLICY="quality",
            OPENAI_TASK_CATEGORIES="general",
            ANTHROPIC_TASK_CATEGORIES="general",
            OPENAI_QUALITY_CLASS="premium",
            ANTHROPIC_QUALITY_CLASS="standard",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(main_mod.router.last_decision.reason, REASON_AUTO_PRIORITY_FALLBACK)
        self._assert_contract(response.json())

    def test_k_error_fallback_unchanged(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="openai,anthropic",
            fallback="error",
            AUTO_ROUTING_POLICY="quality",
            OPENAI_TASK_CATEGORIES="general",
            ANTHROPIC_TASK_CATEGORIES="general",
            OPENAI_QUALITY_CLASS="premium",
            ANTHROPIC_QUALITY_CLASS="standard",
        )
        response, mocks = self._analyze_auto(main_mod, "openai", "anthropic")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "no_capable_provider")
        self.assertEqual(detail["category"], "technical")

    def test_l_explicit_mode_ignores_ranking(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
            AUTO_ROUTING_POLICY="quality",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_QUALITY_CLASS="standard",
            ANTHROPIC_QUALITY_CLASS="premium",
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
        self.assertEqual(main_mod.router.last_decision.reason, REASON_EXPLICIT_PROVIDER)
        self._assert_contract(response.json())

    def test_m_both_ignores_ranking(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
            AUTO_ROUTING_POLICY="quality",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_QUALITY_CLASS="standard",
            ANTHROPIC_QUALITY_CLASS="premium",
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

    def test_n_omitted_mode_is_both(self):
        main_mod = load_capability_app(
            "openai",
            "anthropic",
            auto_order="anthropic,openai",
            AUTO_ROUTING_POLICY="quality",
            OPENAI_TASK_CATEGORIES="general,technical",
            ANTHROPIC_TASK_CATEGORIES="general,technical",
            OPENAI_QUALITY_CLASS="standard",
            ANTHROPIC_QUALITY_CLASS="premium",
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": TECHNICAL_TEXT, "role": "technical"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(
            main_mod.router.last_decision.reason,
            REASON_ALL_AVAILABLE_PROVIDERS,
        )
        self._assert_contract(response.json())

    def test_o_invalid_policy_is_config_error(self):
        with self.assertRaises(InvalidAutoRoutingPolicyError):
            load_capability_app(
                "openai",
                auto_order="openai",
                AUTO_ROUTING_POLICY="foo",
            )


if __name__ == "__main__":
    unittest.main()
