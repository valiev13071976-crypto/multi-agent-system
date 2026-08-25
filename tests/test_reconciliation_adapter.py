import unittest

from side_effects.models import (
    ADAPTER_RECON_NOT_FOUND,
    ADAPTER_RECON_UNKNOWN,
    OUTCOME_KNOWN_FAILURE,
    OUTCOME_KNOWN_SUCCESS,
    OUTCOME_UNCERTAIN,
    RECON_CONFIRMED_FAILED,
    RECON_CONFIRMED_SUCCEEDED,
    RECON_MANUAL_REVIEW,
    RECON_STILL_UNCERTAIN,
)
from tests.side_effect_fixtures import T0, make_uncertain, recon_runtime, se_action


class ReconciliationAdapterTests(unittest.IsolatedAsyncioTestCase):

    async def test_f_lookup_invoked_when_supported(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="adp-f")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(adapter.reconcile_calls, 1)

    async def test_g_unsupported_goes_manual_review(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.set_reconciliation_flags(supports_reconciliation=False)
        action = se_action(workflow_id, idempotency_key="adp-g")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertTrue(outcome.manual_review_required)
        self.assertEqual(outcome.status, RECON_MANUAL_REVIEW)
        self.assertEqual(adapter.calls, 1)

    async def test_h_authoritative_success(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="adp-h")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.status, RECON_CONFIRMED_SUCCEEDED)
        self.assertEqual(outcome.outcome, OUTCOME_KNOWN_SUCCESS)

    async def test_i_authoritative_failure(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.reconcile_override = "failed"
        action = se_action(workflow_id, idempotency_key="adp-i")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.status, RECON_CONFIRMED_FAILED)
        self.assertEqual(outcome.outcome, OUTCOME_KNOWN_FAILURE)

    async def test_j_unknown_stays_uncertain(self):
        engine, workflow_id, adapter, executor, service = recon_runtime(max_attempts=3)
        adapter.reconcile_override = ADAPTER_RECON_UNKNOWN
        action = se_action(workflow_id, idempotency_key="adp-j")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.status, RECON_STILL_UNCERTAIN)
        self.assertEqual(outcome.outcome, OUTCOME_UNCERTAIN)

    async def test_k_non_authoritative_not_confirmed(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.set_reconciliation_flags(reconciliation_authoritative=False)
        action = se_action(workflow_id, idempotency_key="adp-k")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertNotEqual(outcome.status, RECON_CONFIRMED_SUCCEEDED)
        self.assertNotEqual(outcome.status, RECON_CONFIRMED_FAILED)
        self.assertTrue(outcome.manual_review_required)

    async def test_l_not_found_default_uncertain(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.reconcile_override = ADAPTER_RECON_NOT_FOUND
        action = se_action(workflow_id, idempotency_key="adp-l")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.status, RECON_STILL_UNCERTAIN)

    async def test_m_not_found_authoritative_failure(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.set_reconciliation_flags(not_found_is_authoritative_failure=True)
        adapter.reconcile_override = ADAPTER_RECON_NOT_FOUND
        action = se_action(workflow_id, idempotency_key="adp-m")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.status, RECON_CONFIRMED_FAILED)

    async def test_n_not_found_no_auto_retry(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.set_reconciliation_flags(not_found_is_authoritative_failure=True)
        adapter.reconcile_override = ADAPTER_RECON_NOT_FOUND
        action = se_action(workflow_id, idempotency_key="adp-n")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(adapter.calls, 1)


if __name__ == "__main__":
    unittest.main()
