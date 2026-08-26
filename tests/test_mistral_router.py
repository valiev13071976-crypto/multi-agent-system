"""Mistral router selection tests."""

from __future__ import annotations

import unittest

from finops.budget_models import BudgetConstraints
from agents.model_profile import build_model_profile
from agents.model_router import ModelRouter, REASON_AUTO_CAPABILITY_MATCH, REASON_EXPLICIT_PROVIDER
from agents.provider_registry import ProviderRecord, ProviderRegistry


class MistralRouterTests(unittest.TestCase):
    def _reg(self, *, mistral_cats="general,research,technical", openai_cats="general"):
        records = {
            "mistral": ProviderRecord("mistral", "mistral-large-latest", True),
            "openai": ProviderRecord("openai", "gpt-test", True),
            "anthropic": ProviderRecord("anthropic", "claude", False),
            "gemini": ProviderRecord("gemini", "gem", False),
            "grok": ProviderRecord("grok", "grok", False),
            "deepseek": ProviderRecord("deepseek", "ds", False),
            "moonshot": ProviderRecord("moonshot", "kimi", False),
        }
        profiles = {
            "mistral": build_model_profile(
                "mistral",
                "mistral-large-latest",
                task_categories_raw=mistral_cats,
                quality_raw="premium",
                cost_raw="standard",
                latency_raw="standard",
                context_raw="long",
                quality_status="provisional",
                context_window=256_000,
            ),
            "openai": build_model_profile(
                "openai",
                "gpt-test",
                task_categories_raw=openai_cats,
                quality_raw="standard",
            ),
        }
        return ProviderRegistry(
            records,
            profiles=profiles,
            auto_provider_order=("mistral", "openai"),
            auto_routing_policy="quality",
            auto_capability_fallback="general",
        )

    def test_explicit_mistral(self):
        decision = ModelRouter(self._reg()).decide(mode="mistral", role_id="researcher")
        self.assertEqual(decision.provider_ids, ("mistral",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)
        self.assertEqual(decision.models["mistral"], "mistral-large-latest")

    def test_auto_selects_mistral_when_best(self):
        decision = ModelRouter(self._reg()).decide(
            mode="auto", role_id="researcher", category="research"
        )
        self.assertEqual(decision.provider_ids, ("mistral",))
        self.assertEqual(decision.reason, REASON_AUTO_CAPABILITY_MATCH)

    def test_both_skips_disabled_unavailable(self):
        records = {
            "mistral": ProviderRecord("mistral", "mistral-large-latest", False),
            "openai": ProviderRecord("openai", "gpt-test", True),
            "anthropic": ProviderRecord("anthropic", "claude", False),
            "gemini": ProviderRecord("gemini", "gem", False),
            "grok": ProviderRecord("grok", "grok", False),
            "deepseek": ProviderRecord("deepseek", "ds", False),
            "moonshot": ProviderRecord("moonshot", "kimi", False),
        }
        reg = ProviderRegistry(records, auto_provider_order=("mistral", "openai"))
        decision = ModelRouter(reg).decide(mode="both", role_id="strategist")
        self.assertEqual(decision.provider_ids, ("openai",))

    def test_budget_can_exclude_mistral(self):
        constraints = BudgetConstraints(excluded_providers=("mistral",))
        decision = ModelRouter(self._reg()).decide(
            mode="auto",
            role_id="researcher",
            category="research",
            budget_constraints=constraints,
        )
        self.assertNotIn("mistral", decision.provider_ids)

    def test_provisional_quality_not_verified_flag(self):
        profile = self._reg().profile("mistral")
        self.assertEqual(profile.quality_status, "provisional")
        self.assertNotEqual(profile.quality_status, "verified")
        self.assertEqual(profile.context_class, "long")
        self.assertEqual(profile.context_window, 256_000)


if __name__ == "__main__":
    unittest.main()
