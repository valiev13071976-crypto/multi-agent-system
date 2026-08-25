"""Unit tests for sensitive memory encryption behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    MemoryIngestRequest,
    MemoryScope,
)
from memory.service import MemoryEncryptionUnavailable, MemoryService
from memory.sqlite_store import SqliteMemoryStore
from memory.store import InMemoryMemoryStore
from security.encryption import (
    DecryptionError,
    EncryptionService,
    SENSITIVITY_SENSITIVE,
)


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _enc(key=None, key_id="v1"):
    if key is None:
        key = AESGCM.generate_key(bit_length=256)
    return EncryptionService(key=key, key_id=key_id), key


class MemoryEncryptionTests(unittest.TestCase):
    def test_sensitive_sqlite_raw_bytes_no_plaintext(self):
        enc, _ = _enc()
        fixture = "sensitive-fixture-plaintext-xyz"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem.sqlite3"
            store = SqliteMemoryStore(db_path=path)
            svc = MemoryService(store, encryption=enc)
            row = svc.ingest(
                MemoryIngestRequest(
                    scope=_scope(),
                    memory_type=MEMORY_SEMANTIC,
                    content=fixture,
                    source_type=SOURCE_OPERATOR,
                    source_id="op-1",
                    sensitivity=SENSITIVITY_SENSITIVE,
                )
            )
            raw = path.read_bytes()
            store.close()
            self.assertNotIn(fixture.encode("utf-8"), raw)
            self.assertIsNone(row.content_safe)
            self.assertTrue(row.encrypted_content)

    def test_decrypt_round_trip(self):
        enc, _ = _enc()
        svc = MemoryService(InMemoryMemoryStore(), encryption=enc)
        plaintext = "round-trip sensitive note"
        row = svc.ingest(
            MemoryIngestRequest(
                scope=_scope(),
                memory_type=MEMORY_SEMANTIC,
                content=plaintext,
                source_type=SOURCE_OPERATOR,
                source_id="op-2",
                sensitivity=SENSITIVITY_SENSITIVE,
            )
        )
        self.assertIsNone(row.content_safe)
        self.assertEqual(enc.decrypt(row.encrypted_content), plaintext)

    def test_wrong_key_decryption_error(self):
        # Same key_id so lookup succeeds; different key material fails decrypt.
        first, _ = _enc(key_id="v1")
        second, _ = _enc(key_id="v1")
        svc = MemoryService(InMemoryMemoryStore(), encryption=first)
        row = svc.ingest(
            MemoryIngestRequest(
                scope=_scope(),
                memory_type=MEMORY_SEMANTIC,
                content="classified payload",
                source_type=SOURCE_OPERATOR,
                source_id="op-3",
                sensitivity=SENSITIVITY_SENSITIVE,
            )
        )
        with self.assertRaises(DecryptionError):
            second.decrypt(row.encrypted_content)

    def test_no_encryption_service_raises(self):
        svc = MemoryService(InMemoryMemoryStore(), encryption=None)
        with self.assertRaises(MemoryEncryptionUnavailable):
            svc.ingest(
                MemoryIngestRequest(
                    scope=_scope(),
                    memory_type=MEMORY_SEMANTIC,
                    content="needs encryption",
                    source_type=SOURCE_OPERATOR,
                    source_id="op-4",
                    sensitivity=SENSITIVITY_SENSITIVE,
                )
            )


if __name__ == "__main__":
    unittest.main()
