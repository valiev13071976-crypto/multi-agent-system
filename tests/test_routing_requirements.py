"""Offline tests for TaskRequirements schema and inference (P0.1)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from agents.model_router import REASON_EXPLICIT_PROVIDER, ModelRouter
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.routing_requirements import (
    CAPABILITY_CODING,
    CAPABILITY_LONG_CONTEXT,
    CAPABILITY_SEARCH,
    CAPABILITY_VISION,
    COMPLEXITY_SIMPLE,
    COMPLEXITY_STANDARD,
    CONTEXT_LONG,
    CONTEXT_STANDARD,
    FRESHNESS_CURRENT,
    FRESHNESS_STATIC,
    LONG_CONTEXT_CHAR_THRESHOLD,
    RISK_LOW,
    RISK_MEDIUM,
    TaskRequirements,
    conservative_default_requirements,
    derive_task_requirements,
)
from agents.router_v2 import RouterV2
from agents.task_classifier import (
    CATEGORY_GENERAL,
    CATEGORY_RESEARCH,
    CATEGORY_TECHNICAL,
    TaskClassification,
    TaskClassifier,
)


class TaskRequirementsSchemaTests(unittest.TestCase):
    def test_conservative_defaults(self):
        req = conservative_default_requirements()
        self.assertEqual(req.complexity, COMPLEXITY_SIMPLE)
        self.assertEqual(req.freshness, FRESHNESS_STATIC)
        self.assertEqual(req.risk, RISK_LOW)
        self.assertEqual(req.required_capabilities, ())
        self.assertEqual(req.context_requirement, CONTEXT_STANDARD)

    def test_immutable(self):
        req = TaskRequirements()
        with self.assertRaises(Exception):
            req.complexity = COMPLEXITY_STANDARD  # type: ignore[misc]
        with self.assertRaises(TypeError):
            req.as_dict()["complexity"] = "x"  # type: ignore[index]

    def test_unknown_values_normalized(self):
        req = TaskRequirements(
            complexity="nope",
            freshness="nope",
            risk="nope",
            required_capabilities=("coding", "unknown-cap"),
            context_requirement="nope",
        )
        self.assertEqual(req.complexity, COMPLEXITY_SIMPLE)
        self.assertEqual(req.freshness, FRESHNESS_STATIC)
        self.assertEqual(req.risk, RISK_LOW)
        self.assertEqual(req.required_capabilities, (CAPABILITY_CODING,))
        self.assertEqual(req.context_requirement, CONTEXT_STANDARD)


class TaskRequirementsInferenceTests(unittest.TestCase):
    def setUp(self):
        self.classifier = TaskClassifier()

    def test_general_prompt_conservative(self):
        result = self.classifier.classify("привет, как дела?")
        self.assertEqual(result.category, CATEGORY_GENERAL)
        req = result.requirements
        self.assertIsInstance(req, TaskRequirements)
        self.assertEqual(req.complexity, COMPLEXITY_SIMPLE)
        self.assertEqual(req.freshness, FRESHNESS_STATIC)
        self.assertEqual(req.risk, RISK_LOW)
        self.assertEqual(req.required_capabilities, ())
        self.assertEqual(req.context_requirement, CONTEXT_STANDARD)

    def test_technical_coding_prompt(self):
        result = self.classifier.classify("debug this TypeError in app.py")
        self.assertEqual(result.category, CATEGORY_TECHNICAL)
        req = result.requirements
        self.assertEqual(req.complexity, COMPLEXITY_STANDARD)
        self.assertEqual(req.freshness, FRESHNESS_STATIC)
        self.assertEqual(req.risk, RISK_MEDIUM)
        self.assertIn(CAPABILITY_CODING, req.required_capabilities)

    def test_current_research_prompt(self):
        result = self.classifier.classify(
            "найди источники и проверь факты по актуальным данным сейчас"
        )
        self.assertEqual(result.category, CATEGORY_RESEARCH)
        req = result.requirements
        self.assertEqual(req.freshness, FRESHNESS_CURRENT)
        self.assertIn(CAPABILITY_SEARCH, req.required_capabilities)

    def test_static_research_without_freshness_markers(self):
        req = derive_task_requirements(
            category="research",
            text="найди источники и проверь факты по учебнику",
        )
        self.assertEqual(req.freshness, FRESHNESS_STATIC)
        self.assertNotIn(CAPABILITY_SEARCH, req.required_capabilities)

    def test_vision_only_from_metadata(self):
        req = derive_task_requirements(
            category="general",
            text="опиши изображение",
            metadata={"requires_vision": True},
        )
        self.assertIn(CAPABILITY_VISION, req.required_capabilities)

    def test_long_context_only_when_safe(self):
        short = derive_task_requirements(category="general", text="short")
        self.assertEqual(short.context_requirement, CONTEXT_STANDARD)
        long_text = "x" * LONG_CONTEXT_CHAR_THRESHOLD
        long_req = derive_task_requirements(category="general", text=long_text)
        self.assertEqual(long_req.context_requirement, CONTEXT_LONG)
        self.assertIn(CAPABILITY_LONG_CONTEXT, long_req.required_capabilities)

    def test_task_classification_backward_compatible_fields(self):
        result = TaskClassification(
            category=CATEGORY_GENERAL,
            role_id="strategist",
            confidence=0.5,
            reason="general_fallback",
        )
        self.assertEqual(result.category, CATEGORY_GENERAL)
        self.assertEqual(result.role_id, "strategist")
        self.assertEqual(result.confidence, 0.5)
        self.assertEqual(result.reason, "general_fallback")
        self.assertIsInstance(result.requirements, TaskRequirements)


class RouterRequirementsPlumbingTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_passes_requirements_without_changing_selection(self):
        import unittest.mock as mock

        records = {
            "openai": ProviderRecord("openai", "gpt-test", True),
            "anthropic": ProviderRecord("anthropic", "claude", False),
            "gemini": ProviderRecord("gemini", "gem", False),
            "grok": ProviderRecord("grok", "grok", False),
            "deepseek": ProviderRecord("deepseek", "ds", False),
            "moonshot": ProviderRecord("moonshot", "kimi", False),
            "mistral": ProviderRecord("mistral", "mistral-large-latest", False),
        }
        registry = ProviderRegistry(records, auto_provider_order=("openai",))

        with patch.object(RouterV2, "__init__", lambda self: None):
            router = RouterV2()
            router.provider_registry = registry
            router.model_router = ModelRouter(registry)
            # Ensure explicit openai satisfies coding requirement from technical classify.
            from agents.model_profile import build_model_profile

            registry._profiles["openai"] = build_model_profile(
                "openai",
                "gpt-test",
                task_categories_raw="general,technical",
                coding_raw="true",
            )
            router.task_classifier = TaskClassifier()
            router.pipeline = mock.Mock()
            router.pipeline.expert_manager = mock.Mock()
            router.pipeline.expert_manager.get_provider = mock.Mock(return_value=object())
            router.pipeline.execute = mock.AsyncMock(return_value={"ok": True})
            router.last_decision = None
            router.last_classification = None
            router.last_requirements = None
            router.last_route_context = None
            router.last_response_depth = None
            router.last_orchestration_policy = None
            router.last_presentation_policy = None
            router.last_task_id = None
            router.last_workflow_id = None
            router.budget_guard = None
            router.finops = None

            baseline = router.model_router.decide(
                mode="openai",
                role_id="strategist",
                category="general",
            )
            await router.run("debug this TypeError in app.py", mode="openai", role="auto")
            self.assertIsNotNone(router.last_requirements)
            self.assertEqual(router.last_classification.category, CATEGORY_TECHNICAL)
            self.assertIn(CAPABILITY_CODING, router.last_requirements.required_capabilities)
            self.assertEqual(router.last_decision.provider_ids, baseline.provider_ids)
            self.assertEqual(router.last_decision.reason, REASON_EXPLICIT_PROVIDER)
            self.assertIn("requirements", router.last_route_context)
            self.assertEqual(
                router.last_route_context["requirements"]["complexity"],
                COMPLEXITY_STANDARD,
            )


if __name__ == "__main__":
    unittest.main()
