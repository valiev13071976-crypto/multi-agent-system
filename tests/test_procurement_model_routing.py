"""Procurement model routing with Moonshot profiles."""

from __future__ import annotations

import unittest

from agents.model_profile import build_model_profile
from agents.model_router import ModelRouter
from agents.provider_registry import ProviderRecord, ProviderRegistry


class ProcurementModelRoutingTests(unittest.TestCase):
    def test_long_document_prefers_long_context_profile(self):
        records = {
            "moonshot": ProviderRecord("moonshot", "kimi-k3", True),
            "openai": ProviderRecord("openai", "gpt", True),
            "anthropic": ProviderRecord("anthropic", "c", False),
            "gemini": ProviderRecord("gemini", "g", False),
            "grok": ProviderRecord("grok", "x", False),
            "deepseek": ProviderRecord("deepseek", "d", False),
        }
        profiles = {
            "moonshot": build_model_profile(
                "moonshot",
                "kimi-k3",
                task_categories_raw="general,research,technical",
                context_raw="long",
                quality_raw="premium",
                context_window=1_000_000,
            ),
            "openai": build_model_profile(
                "openai",
                "gpt",
                task_categories_raw="general,research",
                context_raw="standard",
                quality_raw="standard",
            ),
        }
        reg = ProviderRegistry(
            records,
            profiles=profiles,
            auto_provider_order=("openai", "moonshot"),
            auto_routing_policy="quality",
        )
        decision = ModelRouter(reg).decide(mode="auto", role_id="researcher", category="research")
        self.assertEqual(decision.provider_ids, ("moonshot",))
        self.assertEqual(reg.profile("moonshot").context_class, "long")

    def test_multilingual_structured_categories(self):
        records = {
            "moonshot": ProviderRecord("moonshot", "kimi-k3", True),
            "openai": ProviderRecord("openai", "gpt", False),
            "anthropic": ProviderRecord("anthropic", "c", False),
            "gemini": ProviderRecord("gemini", "g", False),
            "grok": ProviderRecord("grok", "x", False),
            "deepseek": ProviderRecord("deepseek", "d", False),
        }
        profiles = {
            "moonshot": build_model_profile(
                "moonshot",
                "kimi-k3",
                task_categories_raw="general,technical",
                structured_raw="true",
                multilingual_raw="true",
            ),
        }
        reg = ProviderRegistry(records, profiles=profiles, auto_provider_order=("moonshot",))
        decision = ModelRouter(reg).decide(mode="auto", role_id="technical", category="technical")
        self.assertEqual(decision.provider_ids, ("moonshot",))
        self.assertTrue(reg.profile("moonshot").supports_multilingual)


if __name__ == "__main__":
    unittest.main()
