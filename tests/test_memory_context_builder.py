"""Unit tests for KnowledgeContextBuilder."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from memory.context_builder import KnowledgeContextBuilder
from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_EXTERNAL,
    SOURCE_OPERATOR,
    MemoryProvenance,
    MemoryScope,
    MemorySearchResult,
    citation_ref_for,
)
from security.encryption import SENSITIVITY_INTERNAL, SENSITIVITY_SECRET


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _result(
    memory_id="m1",
    content="safe knowledge snippet",
    sensitivity=SENSITIVITY_INTERNAL,
    source_type=SOURCE_OPERATOR,
):
    stamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    prov = MemoryProvenance(
        source_type=source_type,
        source_id="src-1",
        created_by_component="test",
        ingested_at=stamp,
    )
    return MemorySearchResult(
        memory_id=memory_id,
        score=1.0,
        memory_type=MEMORY_SEMANTIC,
        content_or_summary=content,
        provenance=prov,
        confidence=0.8,
        created_at=stamp,
        source_ref="src-1",
        citation_ref=citation_ref_for(memory_id),
        sensitivity=sensitivity,
        tags=(),
    )


class MemoryContextBuilderTests(unittest.TestCase):
    def test_citation_refs_and_untrusted_flags(self):
        builder = KnowledgeContextBuilder()
        ctx = builder.build((_result("m-cite", content="cited text"),))
        self.assertTrue(ctx.untrusted_data)
        self.assertTrue(ctx.policy_override_forbidden)
        self.assertEqual(len(ctx.items), 1)
        self.assertEqual(ctx.items[0].citation_ref, "memory:m-cite")
        self.assertEqual(ctx.items[0].content, "cited text")

    def test_secret_content_excluded(self):
        builder = KnowledgeContextBuilder()
        secret = _result(
            "m-secret",
            content="super-secret-value",
            sensitivity=SENSITIVITY_SECRET,
            source_type=SOURCE_EXTERNAL,
        )
        safe = _result("m-safe", content="public knowledge")
        ctx = builder.build((secret, safe))
        ids = [i.memory_id for i in ctx.items]
        self.assertNotIn("m-secret", ids)
        self.assertIn("m-safe", ids)
        for item in ctx.items:
            self.assertNotIn("super-secret-value", item.content)


if __name__ == "__main__":
    unittest.main()
