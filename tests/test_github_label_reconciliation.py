import unittest

from side_effects.github.models import OP_ENSURE_ABSENT
from side_effects.models import (
    OUTCOME_UNCERTAIN,
    RECON_CONFIRMED_FAILED,
    RECON_CONFIRMED_SUCCEEDED,
    RECON_MANUAL_REVIEW,
    RECON_STILL_UNCERTAIN,
)
from tests.side_effect_fixtures import (
    T0,
    github_action,
    github_execute,
    github_recon_runtime,
    github_eval_kwargs,
    issue_permit,
)
from side_effects.models import SideEffectExecutionContext


class GitHubLabelReconciliationTests(unittest.IsolatedAsyncioTestCase):

    async def test_aa_uncertain_local_reconcile_confirms_success(self):
        engine, workflow_id, adapter, executor, fake, service = github_recon_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-aa")
        permit = await issue_permit(engine, action)
        context = SideEffectExecutionContext(now=T0)
        context.simulate_finalization_failure = True
        result = await executor.execute(
            action,
            permit=permit,
            context=context,
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=github_eval_kwargs(),
        )
        self.assertEqual(result.outcome, OUTCOME_UNCERTAIN)
        self.assertIn("bug", fake.current("octo", "hello", 1))
        adds_before = fake.add_calls
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.status, RECON_CONFIRMED_SUCCEEDED)
        self.assertEqual(fake.add_calls, adds_before)
        self.assertEqual(fake.remove_calls, 0)

    async def test_ab_intended_present_get_absent_failed(self):
        engine, workflow_id, adapter, executor, fake, service = github_recon_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-ab")
        context = SideEffectExecutionContext(now=T0)
        context.simulate_finalization_failure = True
        permit = await issue_permit(engine, action)
        result = await executor.execute(
            action,
            permit=permit,
            context=context,
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=github_eval_kwargs(),
        )
        fake.seed("octo", "hello", 1, [])
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.status, RECON_CONFIRMED_FAILED)

    async def test_ac_intended_absent_get_present_failed(self):
        engine, workflow_id, adapter, executor, fake, service = github_recon_runtime()
        fake.seed("octo", "hello", 1, ["bug"])
        action = github_action(
            workflow_id, operation=OP_ENSURE_ABSENT, idempotency_key="gh-ac"
        )
        context = SideEffectExecutionContext(now=T0)
        context.simulate_finalization_failure = True
        permit = await issue_permit(engine, action)
        result = await executor.execute(
            action,
            permit=permit,
            context=context,
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=github_eval_kwargs(),
        )
        fake.seed("octo", "hello", 1, ["bug"])
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(outcome.status, RECON_CONFIRMED_FAILED)

    async def test_ad_get_timeout_still_uncertain(self):
        engine, workflow_id, adapter, executor, fake, service = github_recon_runtime(
            recon_timeout=0.05
        )
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-ad")
        context = SideEffectExecutionContext(now=T0)
        context.simulate_finalization_failure = True
        permit = await issue_permit(engine, action)
        result = await executor.execute(
            action,
            permit=permit,
            context=context,
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=github_eval_kwargs(),
        )
        fake.hang_get = True
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertIn(outcome.status, {RECON_STILL_UNCERTAIN, RECON_MANUAL_REVIEW})

    async def test_ae_generic_404_not_confirmed_failure(self):
        engine, workflow_id, adapter, executor, fake, service = github_recon_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-ae")
        context = SideEffectExecutionContext(now=T0)
        context.simulate_finalization_failure = True
        permit = await issue_permit(engine, action)
        result = await executor.execute(
            action,
            permit=permit,
            context=context,
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=github_eval_kwargs(),
        )
        fake.get_status = 404
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertNotEqual(outcome.status, RECON_CONFIRMED_FAILED)

    async def test_af_non_matching_external_ref_conflict(self):
        engine, workflow_id, adapter, executor, fake, service = github_recon_runtime()
        fake.seed("octo", "hello", 1, [])
        fake.seed("octo", "hello", 2, ["bug"])
        action = github_action(workflow_id, idempotency_key="gh-af")
        context = SideEffectExecutionContext(now=T0)
        context.simulate_finalization_failure = True
        permit = await issue_permit(engine, action)
        result = await executor.execute(
            action,
            permit=permit,
            context=context,
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=github_eval_kwargs(),
        )
        other = github_action(
            workflow_id,
            resource="github://octo/hello/issues/2/labels/bug",
            idempotency_key="gh-af-other",
        )
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=other, now=T0)
        self.assertEqual(outcome.status, RECON_MANUAL_REVIEW)

    async def test_ag_reconcile_never_calls_mutation(self):
        engine, workflow_id, adapter, executor, fake, service = github_recon_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-ag")
        context = SideEffectExecutionContext(now=T0)
        context.simulate_finalization_failure = True
        permit = await issue_permit(engine, action)
        result = await executor.execute(
            action,
            permit=permit,
            context=context,
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=github_eval_kwargs(),
        )
        adds = fake.add_calls
        removes = fake.remove_calls
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertEqual(fake.add_calls, adds)
        self.assertEqual(fake.remove_calls, removes)
