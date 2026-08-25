import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autonomy.idempotency import IdempotencyRegistry
from autonomy.models import (
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_FAILED,
    IDEMPOTENCY_RESERVED,
    IDEMPOTENCY_STARTED,
    IDEMPOTENCY_UNCERTAIN,
)
from autonomy.errors import IdempotencyConflictError, IdempotencyTransitionError
from side_effects.sqlite_store import PersistentIdempotencyStore, SqliteConnection


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class IdempotencySqliteStoreTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmpdir.name) / "idem.sqlite3")
        self.conn = SqliteConnection(self.path)
        self.conn.initialize_schema()
        self.store = PersistentIdempotencyStore(self.conn)
        self.registry = IdempotencyRegistry(self.store)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self._tmpdir.cleanup()
        except Exception:
            pass

    def test_o_reserved_persists(self):
        self.registry.reserve("k-o", "a1")
        self.assertEqual(self.registry.get("k-o").state, IDEMPOTENCY_RESERVED)

    def test_p_started_persists(self):
        self.registry.reserve("k-p", "a1")
        self.registry.mark_started("k-p")
        self.assertEqual(self.registry.get("k-p").state, IDEMPOTENCY_STARTED)

    def test_q_completed_persists(self):
        self.registry.reserve("k-q", "a1")
        self.registry.mark_completed("k-q")
        self.assertEqual(self.registry.get("k-q").state, IDEMPOTENCY_COMPLETED)

    def test_r_failed_persists(self):
        self.registry.reserve("k-r", "a1")
        self.registry.mark_started("k-r")
        self.registry.mark_failed("k-r")
        self.assertEqual(self.registry.get("k-r").state, IDEMPOTENCY_FAILED)

    def test_s_uncertain_persists(self):
        self.registry.reserve("k-s", "a1")
        self.registry.mark_started("k-s")
        self.registry.mark_uncertain("k-s")
        self.assertEqual(self.registry.get("k-s").state, IDEMPOTENCY_UNCERTAIN)

    def test_t_invalid_transition_rejected(self):
        self.registry.reserve("k-t", "a1")
        self.registry.mark_completed("k-t")
        with self.assertRaises(IdempotencyTransitionError):
            self.registry.mark_started("k-t")

    def test_u_completed_duplicate_blocked_after_reload(self):
        self.registry.reserve("k-u", "a1")
        self.registry.mark_completed("k-u")
        conn2 = SqliteConnection(self.path)
        conn2.initialize_schema()
        other = PersistentIdempotencyStore(conn2)
        registry2 = IdempotencyRegistry(other)
        with self.assertRaises(IdempotencyConflictError) as ctx:
            registry2.reserve("k-u", "a2")
        self.assertEqual(ctx.exception.reason_code, "duplicate_completed")
        conn2.close()

    def test_v_uncertain_duplicate_blocked_after_reload(self):
        self.registry.reserve("k-v", "a1")
        self.registry.mark_started("k-v")
        self.registry.mark_uncertain("k-v")
        conn2 = SqliteConnection(self.path)
        conn2.initialize_schema()
        registry2 = IdempotencyRegistry(PersistentIdempotencyStore(conn2))
        with self.assertRaises(IdempotencyConflictError) as ctx:
            registry2.reserve("k-v", "a2")
        self.assertEqual(ctx.exception.reason_code, "duplicate_uncertain")
        conn2.close()

if __name__ == "__main__":
    unittest.main()
