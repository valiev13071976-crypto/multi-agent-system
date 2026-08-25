"""Unit tests for InMemoryMemoryStore."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    STATUS_ACTIVE,
    STATUS_DELETED,
    MemoryProvenance,
    MemoryRecord,
    MemoryScope,
)
from memory.store import InMemoryMemoryStore, MemoryVersionConflict, _clone
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


def _record(memory_id="m1", scope=None, content_hash="h1", tags=()):
    stamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return MemoryRecord(
        memory_id=memory_id,
        memory_type=MEMORY_SEMANTIC,
        scope=scope or _scope(),
        content_hash=content_hash,
        source_type=SOURCE_OPERATOR,
        source_ref="src-1",
        provenance=_prov(),
        sensitivity=SENSITIVITY_INTERNAL,
        status=STATUS_ACTIVE,
        created_at=stamp,
        updated_at=stamp,
        content_safe="hello world",
        tags=tags,
        version=1,
    )


class InMemoryMemoryStoreTests(unittest.TestCase):
    def test_create_get_update_delete(self):
        store = InMemoryMemoryStore()
        created = store.create(_record(), _prov(), tags=("alpha",))
        self.assertEqual(created.memory_id, "m1")
        self.assertIn("alpha", created.tags)
        got = store.get("m1")
        self.assertIsNotNone(got)
        self.assertEqual(got.content_safe, "hello world")

        updated = store.update(
            _clone(got, content_safe="updated", updated_at=datetime.now(timezone.utc)),
            expected_version=1,
        )
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.content_safe, "updated")

        deleted = store.delete("m1", expected_version=2)
        self.assertEqual(deleted.status, STATUS_DELETED)
        self.assertIsNone(deleted.content_safe)
        self.assertEqual(store.get("m1").status, STATUS_DELETED)

    def test_scope_queries(self):
        store = InMemoryMemoryStore()
        store.create(_record("a", scope=_scope("p1"), content_hash="h-a"), _prov())
        store.create(_record("b", scope=_scope("p2"), content_hash="h-b"), _prov())
        rows = store.list_by_scope(_scope("p1"))
        self.assertEqual([r.memory_id for r in rows], ["a"])
        active = store.find_active(_scope("p1"), MEMORY_SEMANTIC)
        self.assertEqual(len(active), 1)
        self.assertIsNone(store.find_by_hash(_scope("p2"), MEMORY_SEMANTIC, "h-a"))

    def test_version_conflict(self):
        store = InMemoryMemoryStore()
        row = store.create(_record(), _prov())
        with self.assertRaises(MemoryVersionConflict):
            store.update(_clone(row, content_safe="x"), expected_version=99)
        with self.assertRaises(MemoryVersionConflict):
            store.delete(row.memory_id, expected_version=99)

    def test_atomic_tags_and_provenance(self):
        store = InMemoryMemoryStore()
        row = store.create(_record(tags=("keep",)), _prov(source_id="prov-9"), tags=("extra",))
        self.assertEqual(set(row.tags), {"keep", "extra"})
        prov = store.get_provenance(row.memory_id)
        self.assertIsNotNone(prov)
        self.assertEqual(prov.source_id, "prov-9")
        # Dedup returns existing without orphaning a second provenance entry.
        again = store.create(
            _record(memory_id="m2", content_hash="h1"),
            _prov(source_id="should-not-replace"),
        )
        self.assertEqual(again.memory_id, row.memory_id)
        self.assertEqual(store.get_provenance(row.memory_id).source_id, "prov-9")


if __name__ == "__main__":
    unittest.main()
