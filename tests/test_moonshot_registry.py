"""Moonshot registry / profile tests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agents.model_profile import build_model_profile
from agents.moonshot_registry import build_moonshot_profile_from_env, moonshot_model_registry_snapshot
from agents.moonshot_versions import QUALITY_STATUS_PROVISIONAL
from agents.provider_registry import ProviderRecord, ProviderRegistry


class MoonshotRegistryTests(unittest.TestCase):
    def test_registry_snapshot(self):
        snap = moonshot_model_registry_snapshot()
        self.assertTrue(snap["model_ids_config_driven"])
        self.assertTrue(snap["no_eternal_default_model"])
        self.assertEqual(snap["quality_default"], QUALITY_STATUS_PROVISIONAL)

    def test_profile_capabilities_explicit(self):
        profile = build_moonshot_profile_from_env(
            "kimi-k3",
            context_window=1_000_000,
            reasoning=True,
            vision=False,
            tool_calling=False,
        )
        self.assertTrue(profile.long_context)
        self.assertTrue(profile.reasoning)
        self.assertFalse(profile.vision)
        self.assertFalse(profile.tool_calling)
        self.assertEqual(profile.quality_status, QUALITY_STATUS_PROVISIONAL)

    def test_deprecated_not_active(self):
        records = {
            "moonshot": ProviderRecord("moonshot", "kimi-k3", True),
            "openai": ProviderRecord("openai", "gpt", True),
        }
        profiles = {
            "moonshot": build_model_profile(
                "moonshot",
                "kimi-k3",
                model_state="deprecated",
                quality_status="provisional",
            ),
            "openai": build_model_profile("openai", "gpt"),
        }
        reg = ProviderRegistry(records, profiles=profiles, auto_provider_order=("moonshot", "openai"))
        self.assertFalse(reg.is_active_profile("moonshot"))
        self.assertTrue(reg.is_active_profile("openai"))

    def test_from_env_disabled_by_default(self):
        with patch.dict(
            os.environ,
            {
                "MOONSHOT_ENABLED": "false",
                "MOONSHOT_API_KEY": "secret",
                "MOONSHOT_DEFAULT_MODEL": "kimi-k3",
            },
            clear=False,
        ):
            reg = ProviderRegistry.from_env()
            self.assertFalse(reg.is_available("moonshot"))
            health = reg.moonshot_health()
            self.assertEqual(health["moonshot_status"], "disabled")

    def test_missing_key_blocked_when_enabled(self):
        with patch.dict(
            os.environ,
            {
                "MOONSHOT_ENABLED": "true",
                "MOONSHOT_API_KEY": "",
                "MOONSHOT_DEFAULT_MODEL": "kimi-k3",
            },
            clear=False,
        ):
            reg = ProviderRegistry.from_env()
            self.assertFalse(reg.is_available("moonshot"))
            self.assertEqual(reg.moonshot_health()["moonshot_status"], "blocked")


if __name__ == "__main__":
    unittest.main()
