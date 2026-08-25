"""Unit tests for SqliteDocumentStore durability."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from documents.models import (
    DOC_TXT,
    SOURCE_OPERATOR,
    STATUS_INGESTED,
    DocumentChunkRecord,
    DocumentProvenance,
    DocumentRecord,
)
from documents.sqlite_store import SqliteDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _prov():
    return DocumentProvenance(
        source_type=SOURCE_OPERATOR,
        source_id="src-1",
        ingested_by="test",
        ingested_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_hash="h1",
    )


def _record(document_id="d1", content_hash="h1"):
    stamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return DocumentRecord(
        document_id=document_id,
        scope=_scope(),
        filename_safe="note.txt",
        media_type="text/plain",
        document_type=DOC_TXT,
        size_bytes=12,
        content_hash=content_hash,
        source_type=SOURCE_OPERATOR,
        source_ref="src-1",
        provenance=_prov(),
        sensitivity=SENSITIVITY_INTERNAL,
        status=STATUS_INGESTED,
        created_at=stamp,
        updated_at=stamp,
    )


class DocumentSqliteStoreTests(unittest.TestCase):
    def test_durability_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docs.sqlite3"
            store = SqliteDocumentStore(db_path=path)
            row = store.create(_record(), _prov(), tags=("t1",))
            store.save_chunks(
                row.document_id,
                (
                    DocumentChunkRecord(
                        chunk_id="c1",
                        document_id=row.document_id,
                        scope=_scope(),
                        ordinal=0,
                        content_hash="ch",
                        source_location="txt:0",
                        content_safe="durable chunk text",
                        sensitivity=SENSITIVITY_INTERNAL,
                    ),
                ),
            )
            store.close()

            reopened = SqliteDocumentStore(db_path=path)
            got = reopened.get("d1")
            self.assertIsNotNone(got)
            self.assertEqual(got.filename_safe, "note.txt")
            self.assertEqual(got.content_hash, "h1")
            chunks = reopened.list_chunks("d1")
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0].content_safe, "durable chunk text")
            self.assertIsNotNone(reopened.get_provenance("d1"))
            reopened.close()


if __name__ == "__main__":
    unittest.main()
