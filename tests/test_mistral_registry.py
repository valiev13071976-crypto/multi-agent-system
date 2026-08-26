"""Mistral registry / profile tests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agents.model_profile import build_model_profile
from agents.mistral_registry import build_mistral_profile_from_env, mistral_model_registry_snapshot
from agents.mistral_versions import QUALITY_STATUS_PROVISIONAL
from agents.provider_registry import ProviderRecord, ProviderRegistry


class MistralRegistryTests(unittest.TestCase):
    def test_registry_snapshot(self):
        snap = mistral_model_registry_snapshot()
        self.assertTrue(snap["model_ids_config_driven"])
        self.assertTrue(snap["no_eternal_default_model"])
        self.assertEqual(snap["quality_default"], QUALITY_STATUS_PROVISIONAL)
        self.assertEqual(snap["recommended_general_model"], "mistral-large-latest")
        self.assertEqual(snap["recommended_coding_model"], "codestral-latest")

    def test_profile_capabilities_explicit(self):
        profile = build_mistral_profile_from_env(
            "mistral-large-latest",
            context_window=256_000,
            reasoning=True,
            vision=False,
            tool_calling=False,
            coding=False,
        )
        self.assertTrue(profile.long_context)
        self.assertTrue(profile.reasoning)
        self.assertFalse(profile.vision)
        self.assertFalse(profile.tool_calling)
        self.assertEqual(profile.quality_status, QUALITY_STATUS_PROVISIONAL)

    def test_deprecated_not_active(self):
        records = {
            "mistral": ProviderRecord("mistral", "mistral-large-latest", True),
            "openai": ProviderRecord("openai", "gpt", True),
        }
        profiles = {
            "mistral": build_model_profile(
                "mistral",
                "mistral-large-latest",
                model_state="deprecated",
                quality_status="provisional",
            ),
            "openai": build_model_profile("openai", "gpt"),
        }
        reg = ProviderRegistry(records, profiles=profiles, auto_provider_order=("mistral", "openai"))
        self.assertFalse(reg.is_active_profile("mistral"))
        self.assertTrue(reg.is_active_profile("openai"))

    def test_from_env_disabled_by_default(self):
        with patch.dict(
            os.environ,
            {
                "MISTRAL_ENABLED": "false",
                "MISTRAL_API_KEY": "secret",
                "MISTRAL_DEFAULT_MODEL": "mistral-large-latest",
            },
            clear=False,
        ):
            reg = ProviderRegistry.from_env()
            self.assertFalse(reg.is_available("mistral"))
            health = reg.mistral_health()
            self.assertEqual(health["mistral_status"], "disabled")

    def test_missing_key_blocked_when_enabled(self):
        with patch.dict(
            os.environ,
            {
                "MISTRAL_ENABLED": "true",
                "MISTRAL_API_KEY": "",
                "MISTRAL_DEFAULT_MODEL": "mistral-large-latest",
            },
            clear=False,
        ):
            reg = ProviderRegistry.from_env()
            self.assertFalse(reg.is_available("mistral"))
            self.assertEqual(reg.mistral_health()["mistral_status"], "blocked")


if __name__ == "__main__":
    unittest.main()
