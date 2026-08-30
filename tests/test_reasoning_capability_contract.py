"""Regression: strategy/critique reasoning capability defaults (P0)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agents.capability_match import missing_capabilities
from agents.model_profile import build_model_profile
from agents.model_router import (
    REASON_EXPLICIT_PROVIDER,
    ModelRouter,
    ProviderCapabilityMismatchError,
)
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.routing_requirements import (
    CAPABILITY_REASONING,
    CAPABILITY_SEARCH,
    derive_task_requirements,
)
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


def _registry(profiles: dict, order: tuple[str, ...] | None = None):
    records = {
        pid: ProviderRecord(pid, f"{pid}-m", False)
        for pid in (
            "openai",
            "anthropic",
            "gemini",
            "grok",
            "deepseek",
            "moonshot",
            "mistral",
        )
    }
    full_profiles = {}
    for pid in records:
        if pid in profiles:
            records[pid] = ProviderRecord(pid, profiles[pid].model_id, True)
            full_profiles[pid] = profiles[pid]
        else:
            full_profiles[pid] = build_model_profile(
                pid, f"{pid}-m", task_categories_raw="general", reasoning_raw="false"
            )
    return ProviderRegistry(
        records,
        profiles=full_profiles,
        auto_provider_order=order or tuple(profiles.keys()),
        auto_capability_fallback="error",
        auto_routing_policy="priority",
    )


class ReasoningCapabilityContractTests(unittest.TestCase):
    def test_core_provider_defaults_imply_reasoning(self):
        for provider_id in ("openai", "anthropic", "gemini", "grok", "deepseek"):
            profile = build_model_profile(provider_id, f"{provider_id}-m")
            self.assertIn("strategy", profile.task_categories)
            self.assertIn("critique", profile.task_categories)
            self.assertTrue(
                profile.supports_reasoning,
                msg=f"{provider_id} should infer supports_reasoning",
            )

    def test_moonshot_without_strategy_does_not_imply_reasoning(self):
        profile = build_model_profile("moonshot", "kimi")
        self.assertNotIn("strategy", profile.task_categories)
        self.assertFalse(profile.supports_reasoning)

    def test_explicit_false_overrides_category_inference(self):
        profile = build_model_profile(
            "openai",
            "gpt",
            task_categories_raw="general,strategy,critique",
            reasoning_raw="false",
        )
        self.assertFalse(profile.supports_reasoning)

    def test_general_only_categories_do_not_imply_reasoning(self):
        profile = build_model_profile(
            "openai",
            "gpt",
            task_categories_raw="general",
        )
        self.assertFalse(profile.supports_reasoning)

    def test_strategy_requires_reasoning_general_does_not(self):
        strategy = derive_task_requirements(category="strategy", text="plan")
        critique = derive_task_requirements(category="critique", text="review")
        general = derive_task_requirements(category="general", text="hello")
        self.assertIn(CAPABILITY_REASONING, strategy.required_capabilities)
        self.assertIn(CAPABILITY_REASONING, critique.required_capabilities)
        self.assertNotIn(CAPABILITY_REASONING, general.required_capabilities)

    def test_trend_analysis_does_not_hard_require_search(self):
        req = derive_task_requirements(category="trend_analysis", text="trends")
        self.assertEqual(req.freshness, "current")
        self.assertNotIn(CAPABILITY_SEARCH, req.required_capabilities)

    def test_strategy_reasoning_capable_explicit_pass(self):
        profile = build_model_profile("openai", "gpt")
        self.assertTrue(profile.supports_reasoning)
        reg = _registry({"openai": profile}, order=("openai",))
        decision = ModelRouter(reg).decide(
            mode="openai",
            role_id="strategist",
            category="strategy",
            requirements=derive_task_requirements(category="strategy"),
        )
        self.assertEqual(decision.provider_ids, ("openai",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)

    def test_critique_reasoning_capable_explicit_pass(self):
        profile = build_model_profile("anthropic", "claude")
        reg = _registry({"anthropic": profile}, order=("anthropic",))
        decision = ModelRouter(reg).decide(
            mode="anthropic",
            role_id="critic",
            category="critique",
            requirements=derive_task_requirements(category="critique"),
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)

    def test_provider_without_reasoning_capability_mismatch(self):
        profile = build_model_profile(
            "openai",
            "gpt",
            task_categories_raw="general,strategy",
            reasoning_raw="false",
        )
        self.assertFalse(profile.supports_reasoning)
        missing = missing_capabilities(
            profile,
            derive_task_requirements(category="strategy"),
        )
        self.assertIn(CAPABILITY_REASONING, missing)
        reg = _registry({"openai": profile}, order=("openai",))
        with self.assertRaises(ProviderCapabilityMismatchError) as ctx:
            ModelRouter(reg).decide(
                mode="openai",
                role_id="strategist",
                category="strategy",
                requirements=derive_task_requirements(category="strategy"),
            )
        self.assertIn(CAPABILITY_REASONING, ctx.exception.missing_capabilities)

    def test_analyze_strategist_without_reasoning_env_flag(self):
        """Default strategist path must not 503 from false capability default."""
        main_mod = load_app(
            OPENAI_API_KEY="fake-key",
            OPENAI_MODEL="fake-model",
        )
        profile = main_mod.router.provider_registry.profile("openai")
        self.assertTrue(profile.supports_reasoning)
        with patch.object(
            main_mod.router.pipeline.expert_manager.openai,
            "run",
            new=AsyncMock(return_value="successful strategist answer"),
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for key in CONTRACT_KEYS:
            self.assertIn(key, payload)

    def test_explicit_auto_both_semantics_intact(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            explicit = client.post(
                "/api/analyze",
                json={"prompt": "task", "mode": "openai", "role": "strategist"},
            )
            both = client.post(
                "/api/analyze",
                json={"prompt": "task", "mode": "both", "role": "strategist"},
            )
            auto = client.post(
                "/api/analyze",
                json={"prompt": "task", "mode": "auto", "role": "strategist"},
            )
        self.assertEqual(explicit.status_code, 200)
        self.assertEqual(both.status_code, 200)
        self.assertEqual(auto.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 3)
        self.assertEqual(mocks["anthropic"].await_count, 1)


if __name__ == "__main__":
    unittest.main()
