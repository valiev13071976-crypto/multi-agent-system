"""Eval coverage for P13 memory/knowledge core suite handlers."""

from __future__ import annotations

import unittest

from evals.handlers import get_handler
from evals.models import EvalCase
from evals.versions import CORE_SUITE_VERSION


MEMORY_HANDLERS = (
    "memory_cross_scope_denied",
    "memory_sensitive_encrypted",
    "memory_secret_ingest_denied",
    "memory_deleted_not_retrievable",
    "memory_expired_not_retrievable",
    "memory_same_scope_dedup",
    "memory_unvalidated_not_auto_promoted",
    "memory_poisoning_no_policy_bypass",
    "memory_persistence_fail_closed",
)


class EvalMemoryKnowledgeTests(unittest.TestCase):
    def test_core_suite_version(self):
        self.assertEqual(CORE_SUITE_VERSION, "1.8.0")

    def test_memory_core_handlers_pass(self):
        for name in MEMORY_HANDLERS:
            with self.subTest(name=name):
                case = EvalCase(
                    case_id=name,
                    suite_id="core",
                    case_version="1",
                    category="memory_knowledge",
                    description=name,
                    handler=name,
                    critical=True,
                )
                result = get_handler(name)(case)
                self.assertTrue(result["passed"], msg=f"{name}:{result.get('reason_codes')}")


if __name__ == "__main__":
    unittest.main()
