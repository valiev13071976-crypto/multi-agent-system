from decimal import Decimal
import unittest

from finops.budget_ledger import BudgetLedger, scope_ref
from finops.budget_store import InMemoryBudgetStore


class BudgetLedgerTests(unittest.TestCase):
    def test_totals_and_remaining(self):
        store = InMemoryBudgetStore()
        ledger = BudgetLedger(store)
        store.add_reserved(scope_ref("global"), Decimal("4"))
        store.add_spent(scope_ref("global"), Decimal("3"))
        self.assertEqual(ledger.get_reserved("global"), Decimal("4"))
        self.assertEqual(ledger.get_spent("global"), Decimal("3"))
        self.assertEqual(
            ledger.get_remaining(hard_limit=Decimal("10"), scope="global"),
            Decimal("3"),
        )


if __name__ == "__main__":
    unittest.main()
