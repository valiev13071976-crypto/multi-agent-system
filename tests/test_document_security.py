"""Unit tests for document security deny paths."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from documents.errors import DocumentError
from documents.models import SOURCE_OPERATOR, SOURCE_TEST_FIXTURE, DocumentIngestRequest
from documents.service import DocumentService
from documents.sqlite_store import SqliteDocumentStore
from documents.store import InMemoryDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope
from security.encryption import SENSITIVITY_INTERNAL, SENSITIVITY_SENSITIVE, EncryptionService


def _scope(sid="proj-sec"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class DocumentSecurityTests(unittest.TestCase):
    def test_path_traversal_denied(self):
        svc = DocumentService(InMemoryDocumentStore(), allowed_roots=("/tmp",))
        with self.assertRaises(DocumentError) as ctx:
            svc.ingest_trusted_path(
                "../secret.txt",
                scope=_scope(),
                source_type=SOURCE_OPERATOR,
                source_id="path-1",
            )
        self.assertEqual(ctx.exception.reason, "document_path_denied")

    def test_secret_ingest_denied(self):
        svc = DocumentService(InMemoryDocumentStore())
        with self.assertRaises(DocumentError) as ctx:
            svc.ingest(
                DocumentIngestRequest(
                    scope=_scope(),
                    filename="creds.txt",
                    content=b"Authorization: Bearer sk-abc123secret",
                    source_type=SOURCE_OPERATOR,
                    source_id="sec-1",
                    sensitivity=SENSITIVITY_INTERNAL,
                )
            )
        self.assertEqual(ctx.exception.reason, "document_secret_denied")

    def test_macros_filename_denied(self):
        svc = DocumentService(InMemoryDocumentStore())
        with self.assertRaises(DocumentError) as ctx:
            svc.ingest(
                DocumentIngestRequest(
                    scope=_scope(),
                    filename="macro.xlsm",
                    content=b"PK\x03\x04" + b"\x00" * 64,
                    source_type=SOURCE_TEST_FIXTURE,
                    source_id="macro-1",
                    sensitivity=SENSITIVITY_INTERNAL,
                )
            )
        self.assertEqual(ctx.exception.reason, "document_macros_not_allowed")

    def test_sensitive_encrypted_at_rest(self):
        key = AESGCM.generate_key(bit_length=256)
        enc = EncryptionService(key=key, key_id="v1")
        fixture = "sensitive-document-plaintext-xyz"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docs.sqlite3"
            store = SqliteDocumentStore(db_path=path)
            svc = DocumentService(store, encryption=enc)
            row = svc.ingest(
                DocumentIngestRequest(
                    scope=_scope(),
                    filename="secretish.txt",
                    content=fixture.encode("utf-8"),
                    source_type=SOURCE_OPERATOR,
                    source_id="sens-1",
                    sensitivity=SENSITIVITY_SENSITIVE,
                )
            )
            chunks = store.list_chunks(row.document_id)
            store.close()
            raw = path.read_bytes()
            self.assertNotIn(fixture.encode("utf-8"), raw)
            self.assertTrue(chunks)
            self.assertIsNone(chunks[0].content_safe)
            self.assertTrue(chunks[0].encrypted_content)


if __name__ == "__main__":
    unittest.main()
