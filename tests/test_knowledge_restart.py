"""Restart durability: ingested knowledge via MemoryService SQLite survives reopen."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge.models import (
    SOURCE_MANUAL_REFERENCE,
    TRUST_OPERATOR,
    FreshnessPolicy,
    KnowledgeIngestRequest,
    KnowledgeQuery,
    KnowledgeSource,
)
from knowledge.registry import KnowledgeSourceRegistry
from knowledge.runtime import build_knowledge_runtime
from knowledge.service import KnowledgeService
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from memory.service import MemoryService
from memory.sqlite_store import SqliteMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="restart"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class KnowledgeRestartTests(unittest.TestCase):
    def test_sqlite_ingested_knowledge_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem.sqlite3"
            store = SqliteMemoryStore(db_path=path)
            try:
                mem = MemoryService(store)
                registry = KnowledgeSourceRegistry()
                svc = KnowledgeService(registry, memory_service=mem)
                scope = _scope()
                stamp = utc_now()
                svc.register_source(
                    KnowledgeSource(
                        source_id="manual.default",
                        scope=scope,
                        source_type=SOURCE_MANUAL_REFERENCE,
                        name="Manual",
                        trust_level=TRUST_OPERATOR,
                        refresh_policy=FreshnessPolicy(policy="static"),
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
                item = svc.ingest(
                    KnowledgeIngestRequest(
                        scope=scope,
                        source_id="manual.default",
                        content="restart durable knowledge fact",
                        trust_level=TRUST_OPERATOR,
                        provenance_source_ref="manual:restart",
                        sensitivity=SENSITIVITY_INTERNAL,
                        validated=True,
                    )
                )
                self.assertIsNotNone(item.memory_id)
                memory_id = item.memory_id
            finally:
                store.close()

            store2 = SqliteMemoryStore(db_path=path)
            try:
                mem2 = MemoryService(store2)
                got = mem2.get(memory_id, requesting_scope=scope)
                self.assertIsNotNone(got)
                text = got.content_safe or ""
                self.assertIn("restart durable", text)

                rt = build_knowledge_runtime(
                    memory_service=mem2,
                    freeze=True,
                    default_scope=scope,
                    env={"KNOWLEDGE_ENABLED": "true"},
                )
                self.assertIsNotNone(rt)
                rows = rt.service.retrieve(
                    KnowledgeQuery(query_text="durable knowledge", scope=scope)
                )
                self.assertTrue(any("restart durable" in r.content for r in rows))
            finally:
                store2.close()

    def test_compose_does_not_network_refresh(self):
        from memory.store import InMemoryMemoryStore

        calls = {"n": 0}

        def boom(*_a, **_k):
            calls["n"] += 1
            raise AssertionError("network_forbidden")

        with patch("urllib.request.urlopen", boom), patch(
            "http.client.HTTPConnection", boom
        ):
            rt = build_knowledge_runtime(
                memory_service=MemoryService(InMemoryMemoryStore()),
                freeze=True,
                env={"KNOWLEDGE_ENABLED": "true"},
            )
        self.assertIsNotNone(rt)
        self.assertTrue(rt.registry.frozen)
        self.assertEqual(calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
