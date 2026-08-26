"""Eval coverage for P15 external knowledge / RAG core suite handlers."""

from __future__ import annotations

import unittest

from evals.handlers import get_handler
from evals.models import EvalCase
from evals.versions import CORE_SUITE_VERSION


KNOWLEDGE_HANDLERS = (
    "knowledge_cross_scope_denied",
    "knowledge_arbitrary_url_denied",
    "knowledge_external_via_tool_gateway",
    "knowledge_ssrf_denied",
    "knowledge_disabled_source_excluded",
    "knowledge_stale_handling",
    "knowledge_untrusted_no_policy_override",
    "knowledge_unverified_not_auto_promoted",
    "knowledge_citations_preserved",
    "knowledge_conflicts_returned_separately",
    "knowledge_no_secret_leakage",
    "knowledge_no_vector_db_required",
)


class EvalExternalKnowledgeRagTests(unittest.TestCase):
    def test_core_suite_version(self):
        self.assertEqual(CORE_SUITE_VERSION, "1.8.0")

    def test_knowledge_core_handlers_pass(self):
        for name in KNOWLEDGE_HANDLERS:
            with self.subTest(name=name):
                case = EvalCase(
                    case_id=name,
                    suite_id="core",
                    case_version="1",
                    category="external_knowledge_rag",
                    description=name,
                    handler=name,
                    critical=True,
                )
                result = get_handler(name)(case)
                self.assertTrue(result["passed"], msg=f"{name}:{result.get('reason_codes')}")


if __name__ == "__main__":
    unittest.main()
