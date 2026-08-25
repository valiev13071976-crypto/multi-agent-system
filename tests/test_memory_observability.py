"""Unit tests for memory observability events, metrics labels, and health."""

from __future__ import annotations

import unittest

from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    MemoryIngestRequest,
    MemoryQuery,
    MemoryScope,
)
from memory.runtime import build_memory_runtime
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from observability.runtime import build_observability_runtime
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


FORBIDDEN_LABEL_FRAGMENTS = ("scope_id", "memory_id", "query")


class MemoryObservabilityTests(unittest.TestCase):
    def test_events_metrics_labels_and_health(self):
        obs = build_observability_runtime(env={})
        store = InMemoryMemoryStore()
        svc = MemoryService(store, observability=obs)
        scope = _scope("proj-obs")
        row = svc.ingest(
            MemoryIngestRequest(
                scope=scope,
                memory_type=MEMORY_SEMANTIC,
                content="observability fact token",
                source_type=SOURCE_OPERATOR,
                source_id="op-1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        svc.retrieve(MemoryQuery(query_text="observability fact", scope=scope))
        svc.forget(row.memory_id, requesting_scope=scope, reason="obs")

        events = obs.list_events()
        types = {e.event_type for e in events}
        self.assertIn("memory.ingested", types)
        self.assertIn("memory.retrieved", types)
        self.assertIn("memory.forgotten", types)

        for event in events:
            meta = dict(event.metadata_safe or {})
            blob = str(meta)
            for frag in FORBIDDEN_LABEL_FRAGMENTS:
                self.assertNotIn(frag, meta)
            self.assertNotIn(scope.scope_id, blob)
            self.assertNotIn(row.memory_id, blob)
            self.assertNotIn("observability fact", blob)

        snap = obs.metrics.snapshot()
        self.assertGreaterEqual(snap.get("memory_ingest_total", 0), 1)
        self.assertGreaterEqual(snap.get("memory_retrieval_total", 0), 1)
        self.assertGreaterEqual(snap.get("memory_forget_total", 0), 1)

        by_label = snap.get("by_label", {})
        for counter, buckets in by_label.items():
            if not str(counter).startswith("memory"):
                continue
            joined = " ".join(str(k) for k in buckets.keys())
            for frag in FORBIDDEN_LABEL_FRAGMENTS:
                self.assertNotIn(f"{frag}=", joined)
            self.assertNotIn(scope.scope_id, joined)
            self.assertNotIn(row.memory_id, joined)

        rt = build_memory_runtime(
            env={"MEMORY_ENABLED": "true", "MEMORY_BACKEND": "memory"},
            observability=obs,
        )
        self.assertIsNotNone(rt)
        mem_health = rt.health()
        self.assertIn("memory_status", mem_health)
        self.assertIn(mem_health["memory_status"], {"healthy", "degraded", "blocked"})
        overall = obs.health(
            memory_status=mem_health["memory_status"],
            memory_enabled=True,
            memory_persistence_ready=bool(mem_health.get("persistence_ready", True)),
        )
        self.assertEqual(overall.memory_status, mem_health["memory_status"])
        rt.close()


if __name__ == "__main__":
    unittest.main()
