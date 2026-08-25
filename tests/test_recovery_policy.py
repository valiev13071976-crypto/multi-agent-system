"""Deterministic recovery policy plans."""

from __future__ import annotations

import unittest

from recovery.models import (
    ACTION_RECONCILE_READ_ONLY,
    ACTION_REQUEST_NEW_AUTHORIZATION,
    CASE_UNCERTAIN_SIDE_EFFECT,
    SEVERITY_HIGH,
    STATUS_OPEN,
    RecoveryCase,
    utc_now,
)
from recovery.policy import RecoveryPolicy
from side_effects.models import RECON_CONFIRMED_FAILED, RECON_STILL_UNCERTAIN
from tools.models import TOOL_TRUST_PRIVILEGED, TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE


def _case(**kwargs) -> RecoveryCase:
    stamp = utc_now()
    fields = {
        "recovery_id": "r1",
        "execution_id": "e1",
        "workflow_id": "w",
        "task_id": "t",
        "action_id": "a",
        "tool_id": "tool",
        "operation": "op",
        "case_type": CASE_UNCERTAIN_SIDE_EFFECT,
        "status": STATUS_OPEN,
        "severity": SEVERITY_HIGH,
        "reason_code": "uncertain",
        "created_at": stamp,
        "updated_at": stamp,
        "attempt": 0,
        "max_attempts": 3,
        "tool_trust_level": TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        "version": 1,
    }
    fields.update(kwargs)
    return RecoveryCase(**fields)


class RecoveryPolicyTests(unittest.TestCase):
    def test_uncertain_reconcile_read_only_first(self):
        plan = RecoveryPolicy().plan(_case(), reconciliation_status=None)
        self.assertEqual(plan.steps[0].action_type, ACTION_RECONCILE_READ_ONLY)

    def test_confirmed_failure_new_authorization(self):
        plan = RecoveryPolicy().plan(
            _case(), reconciliation_status=RECON_CONFIRMED_FAILED
        )
        self.assertEqual(plan.steps[0].action_type, ACTION_REQUEST_NEW_AUTHORIZATION)
        self.assertTrue(plan.waiting_operator)

    def test_unknown_waiting_operator_after_max(self):
        plan = RecoveryPolicy().plan(
            _case(attempt=3, max_attempts=3),
            reconciliation_status=RECON_STILL_UNCERTAIN,
        )
        self.assertTrue(plan.waiting_operator)
        self.assertEqual(plan.steps, ())

    def test_financial_like_privileged_waits_operator(self):
        plan = RecoveryPolicy().plan(
            _case(tool_trust_level=TOOL_TRUST_PRIVILEGED),
            reconciliation_status=None,
        )
        self.assertTrue(plan.waiting_operator)


if __name__ == "__main__":
    unittest.main()
