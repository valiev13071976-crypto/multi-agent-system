"""Budget uncertain-cost recovery integration."""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from finops.budget_models import RES_UNCERTAIN, BudgetReservation, utc_now
from recovery.models import CASE_BUDGET_UNCERTAIN_COST
from recovery.orchestrator import RecoveryOrchestrator


class RecoveryBudgetTests(unittest.TestCase):
    def test_uncertain_billing_case_no_zero_assumption(self):
        orch = RecoveryOrchestrator(enqueue_reconcile_on_create=False)
        stamp = utc_now()
        reservation = BudgetReservation(
            reservation_id="res-1",
            scope_refs=("global:global",),
            task_id="t",
            provider="openai",
            model="m",
            estimated_cost=Decimal("1.50"),
            currency="USD",
            status=RES_UNCERTAIN,
            created_at=stamp,
            expires_at=stamp + timedelta(hours=1),
        )

        class BudgetStore:
            def list_by_status(self, status):
                return [reservation] if status == RES_UNCERTAIN else []

        orch.materialize_from_local_scan(
            execution_store=type("E", (), {"list_all": lambda self: []})(),
            budget_store=BudgetStore(),
            enqueue=False,
        )
        cases = [
            c
            for c in orch.list_open_cases()
            if c.case_type == CASE_BUDGET_UNCERTAIN_COST
        ]
        self.assertEqual(len(cases), 1)
        self.assertTrue(cases[0].metadata_safe.get("reservation_retained"))
        self.assertNotEqual(reservation.estimated_cost, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
