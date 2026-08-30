"""FH.15 — search capability contract (model vs tool)."""

from __future__ import annotations

import unittest

from agents.capability_match import (
    MATCH_FAIL,
    MATCH_PASS,
    match_capability,
    missing_capabilities,
    profile_satisfies_requirements,
)
from agents.model_profile import ModelProfile, build_model_profile
from agents.routing_requirements import (
    CAPABILITY_SEARCH,
    TaskRequirements,
    derive_task_requirements,
)


def _profile(**kw) -> ModelProfile:
    base = dict(
        provider_id="openai",
        model_id="m",
        enabled=True,
        quality_class="standard",
        cost_class="standard",
        latency_class="standard",
        task_categories=("general", "research"),
        supports_tools=True,
        supports_vision=False,
        supports_structured_output=False,
        context_class="standard",
        supports_search=False,
    )
    base.update(kw)
    return ModelProfile(**base)


class FHSearchCapabilityTests(unittest.TestCase):
    def test_tools_do_not_imply_search(self):
        profile = _profile(supports_tools=True, supports_search=False)
        self.assertEqual(match_capability(profile, CAPABILITY_SEARCH), MATCH_FAIL)

    def test_capable_route_accepted(self):
        profile = _profile(supports_search=True)
        self.assertEqual(match_capability(profile, CAPABILITY_SEARCH), MATCH_PASS)
        req = TaskRequirements(
            complexity="standard",
            freshness="current",
            risk="low",
            required_capabilities=(CAPABILITY_SEARCH,),
            context_requirement="standard",
        )
        self.assertTrue(profile_satisfies_requirements(profile, req))

    def test_incapable_route_rejected(self):
        profile = _profile(supports_search=False)
        req = TaskRequirements(
            complexity="standard",
            freshness="current",
            risk="low",
            required_capabilities=(CAPABILITY_SEARCH,),
            context_requirement="standard",
        )
        self.assertFalse(profile_satisfies_requirements(profile, req))
        self.assertIn(CAPABILITY_SEARCH, missing_capabilities(profile, req))

    def test_env_supports_search_flag(self):
        profile = build_model_profile("openai", "m", search_raw="true")
        self.assertTrue(profile.supports_search)
        self.assertEqual(match_capability(profile, CAPABILITY_SEARCH), MATCH_PASS)

    def test_research_freshness_may_require_search(self):
        req = derive_task_requirements(
            category="research", text="latest news today about markets"
        )
        self.assertIn(CAPABILITY_SEARCH, req.required_capabilities)


if __name__ == "__main__":
    unittest.main()
