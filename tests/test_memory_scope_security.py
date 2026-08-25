"""Unit tests for memory cross-scope access security."""

from __future__ import annotations

import unittest

from memory.access import MemoryAccessDenied
from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    MemoryIngestRequest,
    MemoryQuery,
    MemoryScope,
)
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class MemoryScopeSecurityTests(unittest.TestCase):
    def test_cross_scope_read_denied(self):
        svc = MemoryService(InMemoryMemoryStore())
        a = _scope("a")
        b = _scope("b")
        svc.ingest(
            MemoryIngestRequest(
                scope=a,
                memory_type=MEMORY_SEMANTIC,
                content="secret project alpha fact",
                source_type=SOURCE_OPERATOR,
                source_id="op-1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        with self.assertRaises(MemoryAccessDenied):
            svc.retrieve(
                MemoryQuery(query_text="alpha", scope=a),
                requesting_scope=b,
            )

    def test_cross_scope_retrieve_emits_memory_denied(self):
        from observability.runtime import build_observability_runtime

        obs = build_observability_runtime(env={})
        svc = MemoryService(InMemoryMemoryStore(), observability=obs)
        a = _scope("scope-alpha-id")
        b = _scope("scope-beta-id")
        query_text = "unique-alpha-query-leak-check"
        content = "cross-scope-content-must-not-leak"
        svc.ingest(
            MemoryIngestRequest(
                scope=a,
                memory_type=MEMORY_SEMANTIC,
                content=content,
                source_type=SOURCE_OPERATOR,
                source_id="op-obs",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        with self.assertRaises(MemoryAccessDenied) as ctx:
            svc.retrieve(
                MemoryQuery(query_text=query_text, scope=a),
                requesting_scope=b,
            )
        self.assertEqual(ctx.exception.reason, "cross_scope_denied")

        denied = [e for e in obs.list_events() if e.event_type == "memory.denied"]
        self.assertEqual(len(denied), 1)
        meta = dict(denied[0].metadata_safe or {})
        self.assertEqual(meta.get("reason_code"), "memory_scope_access_denied")
        self.assertEqual(meta.get("operation"), "read")
        self.assertEqual(meta.get("scope_type"), b.scope_type)
        self.assertEqual(meta.get("target_scope_type"), a.scope_type)
        blob = str(meta)
        self.assertNotIn(a.scope_id, blob)
        self.assertNotIn(b.scope_id, blob)
        self.assertNotIn(query_text, blob)
        self.assertNotIn(content, blob)
        self.assertNotIn("scope_id", meta)

    def test_secret_and_auto_deny_not_duplicated(self):
        from memory.models import SOURCE_EXTERNAL
        from memory.service import MemoryDenied
        from observability.runtime import build_observability_runtime

        obs = build_observability_runtime(env={})
        svc = MemoryService(InMemoryMemoryStore(), observability=obs)
        scope = _scope("s1")
        with self.assertRaises(MemoryDenied):
            svc.ingest(
                MemoryIngestRequest(
                    scope=scope,
                    memory_type=MEMORY_SEMANTIC,
                    content="token=ghp_abcdefghijklmnopqrstuvwxyz012345",
                    source_type=SOURCE_OPERATOR,
                    source_id="op-sec",
                    sensitivity=SENSITIVITY_INTERNAL,
                )
            )
        with self.assertRaises(MemoryDenied):
            svc.ingest(
                MemoryIngestRequest(
                    scope=scope,
                    memory_type=MEMORY_SEMANTIC,
                    content="unverified external claim",
                    source_type=SOURCE_EXTERNAL,
                    source_id="ext-1",
                    sensitivity=SENSITIVITY_INTERNAL,
                ),
                auto=True,
                validated=False,
            )
        denied = [e for e in obs.list_events() if e.event_type == "memory.denied"]
        self.assertEqual(len(denied), 2)
        reasons = {dict(e.metadata_safe or {}).get("reason") for e in denied}
        self.assertEqual(reasons, {"secret_ingest_denied", "auto_store_denied"})
        for event in denied:
            self.assertNotEqual(
                dict(event.metadata_safe or {}).get("reason_code"),
                "memory_scope_access_denied",
            )

    def test_get_returns_none_no_presence_leak(self):
        svc = MemoryService(InMemoryMemoryStore())
        a = _scope("a")
        b = _scope("b")
        row = svc.ingest(
            MemoryIngestRequest(
                scope=a,
                memory_type=MEMORY_SEMANTIC,
                content="hidden presence fact",
                source_type=SOURCE_OPERATOR,
                source_id="op-2",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        # Cross-scope get must not raise and must not reveal presence.
        self.assertIsNone(svc.get(row.memory_id, requesting_scope=b))
        self.assertIsNotNone(svc.get(row.memory_id, requesting_scope=a))
        self.assertIsNone(svc.get("does-not-exist", requesting_scope=b))


if __name__ == "__main__":
    unittest.main()
