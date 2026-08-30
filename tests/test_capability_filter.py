"""P0.2 capability-aware filtering tests — offline only."""

from __future__ import annotations

import unittest

from agents.capability_match import (
    MATCH_FAIL,
    MATCH_PASS,
    MATCH_UNRESOLVED,
    match_capability,
    missing_capabilities,
    profile_satisfies_requirements,
)
from agents.model_profile import build_model_profile
from agents.model_router import (
    REASON_AUTO_CAPABILITY_MATCH,
    REASON_EXPLICIT_PROVIDER,
    ModelRouter,
    NoCapableProviderError,
    ProviderCapabilityMismatchError,
)
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.routing_requirements import (
    CAPABILITY_CODING,
    CAPABILITY_REASONING,
    CAPABILITY_SEARCH,
    CAPABILITY_VISION,
    CONTEXT_LONG,
    TaskRequirements,
)


def _profile(
    provider_id: str,
    *,
    coding=False,
    reasoning=False,
    vision=False,
    tools=False,
    context_class="standard",
    context_window=None,
    categories="general",
):
    return build_model_profile(
        provider_id,
        f"{provider_id}-m",
        task_categories_raw=categories,
        coding_raw="true" if coding else "false",
        reasoning_raw="true" if reasoning else "false",
        vision_raw="true" if vision else "false",
        tools_raw="true" if tools else "false",
        context_raw=context_class,
        context_window=context_window,
    )


def _registry(profiles: dict, *, available=None, order=None, fallback="error"):
    available = available or {pid: True for pid in profiles}
    records = {
        pid: ProviderRecord(pid, f"{pid}-m", bool(available.get(pid, False)))
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
    for pid, profile in profiles.items():
        records[pid] = ProviderRecord(pid, profile.model_id, True)
    full_profiles = {}
    for pid in records:
        full_profiles[pid] = profiles.get(
            pid,
            _profile(pid, categories="general"),
        )
    return ProviderRegistry(
        records,
        profiles=full_profiles,
        auto_provider_order=order or tuple(profiles.keys()),
        auto_capability_fallback=fallback,
        auto_routing_policy="priority",
    )


class CapabilityMatchUnitTests(unittest.TestCase):
    def test_coding_mapping(self):
        self.assertEqual(
            match_capability(_profile("openai", coding=True), CAPABILITY_CODING),
            MATCH_PASS,
        )
        self.assertEqual(
            match_capability(_profile("openai", coding=False), CAPABILITY_CODING),
            MATCH_FAIL,
        )

    def test_search_not_inferred_from_tools(self):
        profile = _profile("openai", tools=True, coding=True)
        self.assertEqual(match_capability(profile, CAPABILITY_SEARCH), MATCH_FAIL)
        self.assertFalse(
            profile_satisfies_requirements(
                profile,
                TaskRequirements(required_capabilities=(CAPABILITY_SEARCH,)),
            )
        )
        capable = build_model_profile(
            "openai",
            "openai-m",
            search_raw="true",
            tools_raw="false",
        )
        self.assertEqual(match_capability(capable, CAPABILITY_SEARCH), MATCH_PASS)

    def test_long_context_from_class_and_window(self):
        by_class = _profile("openai", context_class="long")
        self.assertTrue(
            profile_satisfies_requirements(
                by_class,
                TaskRequirements(context_requirement=CONTEXT_LONG),
            )
        )
        by_window = _profile("openai", context_window=128_000)
        self.assertTrue(
            profile_satisfies_requirements(
                by_window,
                TaskRequirements(context_requirement=CONTEXT_LONG),
            )
        )
        short = _profile("openai", context_class="standard", context_window=8000)
        self.assertFalse(
            profile_satisfies_requirements(
                short,
                TaskRequirements(context_requirement=CONTEXT_LONG),
            )
        )


class CapabilityFilterRouterTests(unittest.TestCase):
    def test_coding_rejects_incapable(self):
        reg = _registry(
            {
                "openai": _profile("openai", coding=False, categories="general,technical"),
                "anthropic": _profile(
                    "anthropic", coding=True, categories="general,technical"
                ),
            },
            order=("openai", "anthropic"),
        )
        req = TaskRequirements(required_capabilities=(CAPABILITY_CODING,))
        decision = ModelRouter(reg).decide(
            mode="auto",
            role_id="technical",
            category="technical",
            requirements=req,
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))
        self.assertEqual(decision.reason, REASON_AUTO_CAPABILITY_MATCH)

    def test_coding_accepts_capable(self):
        reg = _registry(
            {"openai": _profile("openai", coding=True, categories="general,technical")},
            order=("openai",),
        )
        decision = ModelRouter(reg).decide(
            mode="auto",
            role_id="technical",
            category="technical",
            requirements=TaskRequirements(required_capabilities=(CAPABILITY_CODING,)),
        )
        self.assertEqual(decision.provider_ids, ("openai",))

    def test_reasoning_filter(self):
        reg = _registry(
            {
                "openai": _profile(
                    "openai", reasoning=False, categories="general,strategy"
                ),
                "anthropic": _profile(
                    "anthropic", reasoning=True, categories="general,strategy"
                ),
            },
            order=("openai", "anthropic"),
        )
        decision = ModelRouter(reg).decide(
            mode="auto",
            role_id="strategist",
            category="strategy",
            requirements=TaskRequirements(required_capabilities=(CAPABILITY_REASONING,)),
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))

    def test_vision_filter(self):
        reg = _registry(
            {
                "openai": _profile("openai", vision=False, categories="general"),
                "gemini": _profile("gemini", vision=True, categories="general"),
            },
            order=("openai", "gemini"),
        )
        decision = ModelRouter(reg).decide(
            mode="auto",
            role_id="strategist",
            category="general",
            requirements=TaskRequirements(required_capabilities=(CAPABILITY_VISION,)),
        )
        self.assertEqual(decision.provider_ids, ("gemini",))

    def test_long_context_filter(self):
        reg = _registry(
            {
                "openai": _profile("openai", context_class="standard", categories="general"),
                "anthropic": _profile(
                    "anthropic", context_class="long", categories="general"
                ),
            },
            order=("openai", "anthropic"),
        )
        decision = ModelRouter(reg).decide(
            mode="auto",
            role_id="strategist",
            category="general",
            requirements=TaskRequirements(context_requirement=CONTEXT_LONG),
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))

    def test_multiple_capabilities_require_all(self):
        reg = _registry(
            {
                "openai": _profile(
                    "openai", coding=True, reasoning=False, categories="general,technical"
                ),
                "anthropic": _profile(
                    "anthropic",
                    coding=True,
                    reasoning=True,
                    categories="general,technical",
                ),
            },
            order=("openai", "anthropic"),
        )
        decision = ModelRouter(reg).decide(
            mode="auto",
            role_id="technical",
            category="technical",
            requirements=TaskRequirements(
                required_capabilities=(CAPABILITY_CODING, CAPABILITY_REASONING)
            ),
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))

    def test_empty_requirements_preserves_auto(self):
        reg = _registry(
            {
                "openai": _profile("openai", coding=False, categories="general,strategy"),
                "anthropic": _profile(
                    "anthropic", coding=True, categories="general,technical"
                ),
            },
            order=("openai", "anthropic"),
        )
        with_empty = ModelRouter(reg).decide(
            mode="auto",
            role_id="strategist",
            category="strategy",
            requirements=TaskRequirements(),
        )
        without = ModelRouter(reg).decide(
            mode="auto",
            role_id="strategist",
            category="strategy",
            requirements=None,
        )
        self.assertEqual(with_empty.provider_ids, without.provider_ids)
        self.assertEqual(with_empty.provider_ids, ("openai",))
        self.assertEqual(with_empty.reason, REASON_AUTO_CAPABILITY_MATCH)

    def test_no_capable_candidate_structured_failure(self):
        reg = _registry(
            {
                "openai": _profile("openai", coding=False, categories="general,technical"),
            },
            order=("openai",),
            fallback="error",
        )
        with self.assertRaises(NoCapableProviderError) as ctx:
            ModelRouter(reg).decide(
                mode="auto",
                role_id="technical",
                category="technical",
                requirements=TaskRequirements(required_capabilities=(CAPABILITY_CODING,)),
            )
        self.assertEqual(ctx.exception.reason, "requirements")
        self.assertIn(CAPABILITY_CODING, ctx.exception.missing_capabilities)

    def test_explicit_incapable_does_not_reroute(self):
        reg = _registry(
            {
                "openai": _profile("openai", coding=False, categories="general"),
                "anthropic": _profile(
                    "anthropic", coding=True, categories="general,technical"
                ),
            },
            order=("openai", "anthropic"),
        )
        with self.assertRaises(ProviderCapabilityMismatchError) as ctx:
            ModelRouter(reg).decide(
                mode="openai",
                role_id="technical",
                category="technical",
                requirements=TaskRequirements(required_capabilities=(CAPABILITY_CODING,)),
            )
        self.assertEqual(ctx.exception.provider, "openai")
        self.assertIn(CAPABILITY_CODING, ctx.exception.missing_capabilities)

    def test_explicit_capable_works(self):
        reg = _registry(
            {"openai": _profile("openai", coding=True, categories="general")},
            order=("openai",),
        )
        decision = ModelRouter(reg).decide(
            mode="openai",
            role_id="technical",
            category="technical",
            requirements=TaskRequirements(required_capabilities=(CAPABILITY_CODING,)),
        )
        self.assertEqual(decision.provider_ids, ("openai",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)

    def test_both_ignores_requirements_filter(self):
        reg = _registry(
            {
                "openai": _profile("openai", coding=False, categories="general"),
                "anthropic": _profile("anthropic", coding=True, categories="general"),
            },
            order=("openai", "anthropic"),
        )
        decision = ModelRouter(reg).decide(
            mode="both",
            role_id="technical",
            category="technical",
            requirements=TaskRequirements(required_capabilities=(CAPABILITY_CODING,)),
        )
        self.assertEqual(decision.provider_ids, ("openai", "anthropic"))

    def test_search_requirement_unsupported(self):
        reg = _registry(
            {
                "openai": _profile(
                    "openai", tools=True, coding=True, categories="general,research"
                ),
            },
            order=("openai",),
            fallback="error",
        )
        with self.assertRaises(NoCapableProviderError):
            ModelRouter(reg).decide(
                mode="auto",
                role_id="researcher",
                category="research",
                requirements=TaskRequirements(required_capabilities=(CAPABILITY_SEARCH,)),
            )
        missing = missing_capabilities(
            _profile("openai", tools=True),
            TaskRequirements(required_capabilities=(CAPABILITY_SEARCH,)),
        )
        self.assertIn(CAPABILITY_SEARCH, missing)


if __name__ == "__main__":
    unittest.main()
