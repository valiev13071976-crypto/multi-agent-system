"""Permit consumed before mutation confirmed."""

from __future__ import annotations

import unittest

from hitl.models import PERMIT_CONSUMED
from recovery.models import CASE_PERMIT_CONSUMED_BEFORE_MUTATION
from recovery.orchestrator import RecoveryOrchestrator
from side_effects.errors import SideEffectAuthorizationError
from side_effects.recovery import RecoveryPolicy


class RecoveryPermitCrashTests(unittest.TestCase):
    def test_case_created_and_permit_not_reused(self):
        orch = RecoveryOrchestrator(enqueue_reconcile_on_create=False)

        class Permit:
            permit_id = "p-crash"
            status = PERMIT_CONSUMED
            workflow_id = "w"
            metadata = {"mutation_unconfirmed": True}

        class PermitStore:
            def list_by_status(self, status):
                return [Permit()] if status == PERMIT_CONSUMED else []

        result = orch.materialize_from_local_scan(
            execution_store=type("E", (), {"list_all": lambda self: []})(),
            permit_store=PermitStore(),
            enqueue=False,
        )
        self.assertEqual(result["network_calls"], 0)
        open_cases = orch.list_open_cases()
        self.assertTrue(
            any(c.case_type == CASE_PERMIT_CONSUMED_BEFORE_MUTATION for c in open_cases)
        )
        with self.assertRaises(SideEffectAuthorizationError):
            RecoveryPolicy().require_fresh_authorization(permit=Permit())


if __name__ == "__main__":
    unittest.main()
