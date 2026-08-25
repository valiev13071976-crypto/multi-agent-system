from datetime import timedelta
from decimal import Decimal
import tempfile
import unittest
from pathlib import Path

from finops.budget_guard import BudgetGuard, BudgetGuardError
from finops.budget_ledger import BudgetLedger
from finops.budget_models import (
    RES_RECONCILED,
    RES_RELEASED,
    BudgetPolicy,
    SCOPE_GLOBAL,
    utc_now,
)
from finops.budget_store import InMemoryBudgetStore, SqliteBudgetStore
from finops.models import BudgetLimits, PriceQuote
from finops.service import FinOpsService


QUOTE = PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)


class BudgetReservationLedgerTests(unittest.TestCase):
    def _guard(self, store=None, limit=Decimal("20")):
        finops = FinOpsService(
            prices={("openai", "m"): QUOTE},
            limits=BudgetLimits(None, None, None, "allow"),
        )
        return BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=limit),),
            store=store or InMemoryBudgetStore(),
            required=True,
        )

    def test_reserve_commit_reconcile_release(self):
        guard = self._guard()
        r = guard.reserve(task_id="t", provider="openai", model="m", estimated_cost=Decimal("5"))
        self.assertEqual(guard.ledger.get_reserved("global"), Decimal("5"))
        done = guard.reconcile(r.reservation_id, actual_cost=Decimal("3"))
        self.assertEqual(done.status, RES_RECONCILED)
        self.assertEqual(guard.ledger.get_spent("global"), Decimal("3"))
        self.assertEqual(guard.ledger.get_reserved("global"), Decimal("0"))

    def test_actual_greater_than_estimate(self):
        guard = self._guard(limit=Decimal("10"))
        r = guard.reserve(task_id="t", provider="openai", model="m", estimated_cost=Decimal("3"))
        guard.reconcile(r.reservation_id, actual_cost=Decimal("5"))
        self.assertEqual(guard.ledger.get_spent("global"), Decimal("5"))

    def test_release_idempotent(self):
        guard = self._guard()
        r = guard.reserve(task_id="t", provider="openai", model="m", estimated_cost=Decimal("4"))
        a = guard.release(r.reservation_id)
        b = guard.release(r.reservation_id)
        self.assertEqual(a.status, RES_RELEASED)
        self.assertEqual(b.status, RES_RELEASED)
        self.assertEqual(guard.ledger.get_reserved("global"), Decimal("0"))

    def test_expiry_releases(self):
        guard = self._guard()
        r = guard.reserve(task_id="t", provider="openai", model="m", estimated_cost=Decimal("4"))
        # force expire
        from finops.budget_models import BudgetReservation

        expired = BudgetReservation(
            reservation_id=r.reservation_id,
            scope_refs=r.scope_refs,
            task_id=r.task_id,
            provider=r.provider,
            model=r.model,
            estimated_cost=r.estimated_cost,
            currency=r.currency,
            status=r.status,
            created_at=r.created_at,
            expires_at=utc_now() - timedelta(seconds=1),
            agent_id=r.agent_id,
            version=r.version,
        )
        guard.store.update_reservation(expired, expected_version=r.version)
        guard.ledger.expire_stale(now=utc_now())
        self.assertEqual(guard.ledger.get_reserved("global"), Decimal("0"))

    def test_sqlite_persistence_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.db"
            store = SqliteBudgetStore(path)
            guard = self._guard(store=store)
            r = guard.reserve(task_id="t", provider="openai", model="m", estimated_cost=Decimal("6"))
            store.close()
            store2 = SqliteBudgetStore(path)
            guard2 = self._guard(store=store2)
            loaded = store2.get_reservation(r.reservation_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(guard2.ledger.get_reserved("global"), Decimal("6"))
            store2.close()


if __name__ == "__main__":
    unittest.main()
