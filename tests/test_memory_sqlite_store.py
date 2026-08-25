"""Unit tests for SqliteMemoryStore durability and FTS fallback."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    STATUS_ACTIVE,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
)
from memory.retrieval import MemoryRetriever
from memory.sqlite_store import SqliteMemoryStore
from memory.store import MemoryVersionConflict, _clone
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _prov(source_id="src-1"):
    return MemoryProvenance(
        source_type=SOURCE_OPERATOR,
        source_id=source_id,
        created_by_component="test",
        ingested_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_hash="hash",
    )


def _record(memory_id="m1", content_hash="h1", content="durable fact about pandas"):
    stamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return MemoryRecord(
        memory_id=memory_id,
        memory_type=MEMORY_SEMANTIC,
        scope=_scope(),
        content_hash=content_hash,
        source_type=SOURCE_OPERATOR,
        source_ref="src-1",
        provenance=_prov(),
        sensitivity=SENSITIVITY_INTERNAL,
        status=STATUS_ACTIVE,
        created_at=stamp,
        updated_at=stamp,
        content_safe=content,
        version=1,
    )


class SqliteMemoryStoreTests(unittest.TestCase):
    def test_durability_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem.sqlite3"
            store = SqliteMemoryStore(db_path=path)
            store.create(_record(), _prov())
            store.close()

            reopened = SqliteMemoryStore(db_path=path)
            got = reopened.get("m1")
            self.assertIsNotNone(got)
            self.assertEqual(got.content_safe, "durable fact about pandas")
            self.assertIsNotNone(reopened.get_provenance("m1"))
            reopened.close()

    def test_version_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem.sqlite3"
            store = SqliteMemoryStore(db_path=path)
            row = store.create(_record(), _prov())
            with self.assertRaises(MemoryVersionConflict):
                store.update(_clone(row, content_safe="x"), expected_version=99)
            store.close()

    def test_atomic_ingest_no_orphan_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem.sqlite3"
            store = SqliteMemoryStore(db_path=path)
            row = store.create(_record(), _prov(source_id="atomic"))
            self.assertIsNotNone(store.get(row.memory_id))
            self.assertEqual(store.get_provenance(row.memory_id).source_id, "atomic")

            # Duplicate content/hash returns existing; no second provenance row created.
            again = store.create(
                _record(memory_id="m-other", content_hash="h1"),
                _prov(source_id="orphan-candidate"),
            )
            self.assertEqual(again.memory_id, row.memory_id)
            self.assertEqual(store.get_provenance(row.memory_id).source_id, "atomic")
            # Only one provenance for the active memory_id.
            self.assertIsNone(store.get_provenance("m-other"))
            store.close()

    def test_fts_unavailable_does_not_crash_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem.sqlite3"
            store = SqliteMemoryStore(db_path=path)
            store.create(_record(), _prov())
            store.fts_available = False
            retriever = MemoryRetriever()
            hits = retriever.search(
                store,
                MemoryQuery(query_text="pandas", scope=_scope()),
            )
            self.assertTrue(retriever.fts_fallback_active)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].memory_id, "m1")
            store.close()


if __name__ == "__main__":
    unittest.main()
