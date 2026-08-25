"""Unit tests for concurrent sqlite memory ingest dedup."""

from __future__ import annotations

import gc
import tempfile
import threading
import unittest
from pathlib import Path

from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    MemoryIngestRequest,
    MemoryScope,
)
from memory.service import MemoryService
from memory.sqlite_store import SqliteMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class MemoryConcurrencyTests(unittest.TestCase):
    def test_two_threads_identical_sqlite_ingest_one_active(self):
        # ignore_cleanup_errors: worker threads may leave sqlite handles on Windows.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "mem.sqlite3"
            store = SqliteMemoryStore(db_path=path)
            svc = MemoryService(store)
            scope = _scope()
            req = MemoryIngestRequest(
                scope=scope,
                memory_type=MEMORY_SEMANTIC,
                content="concurrent identical content",
                source_type=SOURCE_OPERATOR,
                source_id="op-concurrent",
                sensitivity=SENSITIVITY_INTERNAL,
            )
            barrier = threading.Barrier(2)
            results = []
            errors = []
            close_gate = threading.Barrier(2)

            def worker():
                try:
                    barrier.wait(timeout=5)
                    results.append(svc.ingest(req))
                except Exception as exc:  # noqa: BLE001 — collect for assertion
                    errors.append(exc)
                finally:
                    # Close this thread's connection before process teardown.
                    try:
                        close_gate.wait(timeout=5)
                        store.close()
                    except Exception:
                        pass

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0].memory_id, results[1].memory_id)
            active = store.find_active(scope, MEMORY_SEMANTIC)
            self.assertEqual(len(active), 1)
            store.close()
            gc.collect()


if __name__ == "__main__":
    unittest.main()
