"""Unit tests for memory retention / TTL behavior."""

from __future__ import annotations

import unittest
from datetime import timedelta

from memory.models import (
    MEMORY_SEMANTIC,
    MEMORY_WORKING_REFERENCE,
    SCOPE_PROJECT,
    SOURCE_SYSTEM,
    MemoryIngestRequest,
    MemoryQuery,
    MemoryScope,
    utc_now,
)
from memory.retention import MemoryRetentionPolicy
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class MemoryRetentionTests(unittest.TestCase):
    def test_expired_not_retrieved(self):
        svc = MemoryService(InMemoryMemoryStore())
        scope = _scope()
        past = utc_now() - timedelta(hours=2)
        row = svc.ingest(
            MemoryIngestRequest(
                scope=scope,
                memory_type=MEMORY_WORKING_REFERENCE,
                content="short lived ref token",
                source_type=SOURCE_SYSTEM,
                source_id="sys-1",
                sensitivity=SENSITIVITY_INTERNAL,
                retention_ttl_seconds=1,
            ),
            now=past,
        )
        hits = svc.retrieve(MemoryQuery(query_text="short lived", scope=scope))
        self.assertFalse(any(h.memory_id == row.memory_id for h in hits))

    def test_working_reference_short_ttl(self):
        policy = MemoryRetentionPolicy(working_reference_ttl_hours=24)
        now = utc_now()
        expires = policy.expires_at(
            memory_type=MEMORY_WORKING_REFERENCE,
            now=now,
        )
        self.assertIsNotNone(expires)
        delta = expires - now
        self.assertAlmostEqual(delta.total_seconds(), 24 * 3600, delta=1)

    def test_semantic_longer_ttl(self):
        policy = MemoryRetentionPolicy(
            working_reference_ttl_hours=24,
            semantic_ttl_days=3650,
        )
        now = utc_now()
        working = policy.expires_at(memory_type=MEMORY_WORKING_REFERENCE, now=now)
        semantic = policy.expires_at(memory_type=MEMORY_SEMANTIC, now=now)
        self.assertIsNotNone(working)
        self.assertIsNotNone(semantic)
        self.assertGreater(semantic - now, working - now)
        self.assertGreater((semantic - now).days, 100)


if __name__ == "__main__":
    unittest.main()
