import unittest

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from hitl.errors import ActionIntegrityError
from side_effects.errors import (
    RollbackExecutionError,
    SideEffectAuthorizationError,
    SideEffectExecutionError,
)
from side_effects.github.models import OP_ENSURE_ABSENT, OP_ENSURE_PRESENT
from side_effects.models import (
    OUTCOME_UNCERTAIN,
    ROLLBACK_SUCCEEDED,
    STATUS_SUCCEEDED,
    SideEffectExecutionContext,
)
from tests.side_effect_fixtures import (
    T0,
    github_action,
    github_eval_kwargs,
    github_execute,
    github_runtime,
    issue_permit,
)
from tools.models import TOOL_TRUST_INTERNAL_SAFE


class GitHubLabelAdapterTests(unittest.IsolatedAsyncioTestCase):

    async def test_q_ensure_present_adds_once(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id)
        result = await github_execute(executor, action, engine)
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(fake.add_calls, 1)
        self.assertEqual(fake.remove_calls, 0)
        self.assertIn("bug", fake.current("octo", "hello", 1))
        self.assertTrue(result.metadata["changed_by_execution"])
        self.assertTrue(result.metadata["verification_performed"])

    async def test_r_ensure_present_already_present_no_mutation(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, ["bug"])
        action = github_action(workflow_id)
        result = await github_execute(executor, action, engine)
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(fake.add_calls, 0)
        self.assertFalse(result.metadata["changed_by_execution"])

    async def test_s_ensure_absent_removes_once(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, ["bug"])
        action = github_action(workflow_id, operation=OP_ENSURE_ABSENT)
        result = await github_execute(executor, action, engine)
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(fake.remove_calls, 1)
        self.assertEqual(fake.add_calls, 0)
        self.assertNotIn("bug", fake.current("octo", "hello", 1))

    async def test_t_ensure_absent_already_absent_no_mutation(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, operation=OP_ENSURE_ABSENT)
        result = await github_execute(executor, action, engine)
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(fake.remove_calls, 0)
        self.assertFalse(result.metadata["changed_by_execution"])

    async def test_u_completed_idempotency_no_second_mutation(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-once")
        first = await github_execute(executor, action, engine)
        second = await github_execute(executor, action, engine)
        self.assertEqual(first.status, STATUS_SUCCEEDED)
        self.assertEqual(second.execution_id, first.execution_id)
        self.assertEqual(fake.add_calls, 1)
        self.assertEqual(adapter.calls, 1)

    async def test_v_no_authorization_no_http(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        action = github_action(workflow_id)
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.execute(
                action,
                context=SideEffectExecutionContext(now=T0),
                gate=engine._gate(),
                state_manager=engine.state_manager,
            )
        self.assertEqual(fake.get_calls, 0)
        self.assertEqual(adapter.calls, 0)

    async def test_w_invalid_permit_no_http(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        action = github_action(workflow_id)
        permit = await issue_permit(engine, action)
        consumed = engine._hitl().consume_for_execution(
            permit.permit_id, action=action, now=T0
        )
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.execute(
                action,
                permit=consumed,
                context=SideEffectExecutionContext(now=T0),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=github_eval_kwargs(),
            )
        self.assertEqual(fake.get_calls, 0)
        self.assertEqual(adapter.calls, 0)

    async def test_x_fingerprint_mismatch_no_http(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        action = github_action(workflow_id)
        permit = await issue_permit(engine, action)
        other = github_action(
            workflow_id,
            action_id=action.action_id,
            resource="github://octo/hello/issues/1/labels/other",
            idempotency_key=action.idempotency_key,
        )
        with self.assertRaises((SideEffectAuthorizationError, ActionIntegrityError)):
            await executor.execute(
                other,
                permit=permit,
                context=SideEffectExecutionContext(now=T0),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=github_eval_kwargs(),
            )
        self.assertEqual(fake.get_calls, 0)

    async def test_y_capability_mismatch_no_http(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        action = github_action(
            workflow_id, requested_capabilities=(CAP_EXTERNAL_WRITE,)
        )
        permit = await issue_permit(engine, action)
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.execute(
                action,
                permit=permit,
                context=SideEffectExecutionContext(now=T0),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=github_eval_kwargs(
                    capabilities=(CAP_EXTERNAL_WRITE,)
                ),
            )
        self.assertEqual(fake.get_calls, 0)
        self.assertEqual(adapter.calls, 0)

    async def test_z_trust_mismatch_no_http(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        action = github_action(
            workflow_id, tool_trust_level=TOOL_TRUST_INTERNAL_SAFE, risk_class="low"
        )
        permit = await issue_permit(engine, action)
        with self.assertRaises(Exception):
            await executor.execute(
                action,
                permit=permit,
                context=SideEffectExecutionContext(now=T0),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=github_eval_kwargs(),
            )
        self.assertEqual(fake.get_calls, 0)

    async def test_m_repo_not_allowlisted_no_http(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        action = github_action(
            workflow_id, resource="github://other/repo/issues/1/labels/bug"
        )
        with self.assertRaises(SideEffectExecutionError):
            await github_execute(executor, action, engine)
        self.assertEqual(fake.get_calls, 0)

    async def test_ao_401_authentication(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.get_status = 401
        action = github_action(workflow_id)
        with self.assertRaises(SideEffectExecutionError) as caught:
            await github_execute(executor, action, engine)
        self.assertEqual(caught.exception.error_code, "github_authentication_failed")

    async def test_ap_403_permission(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.get_status = 403
        action = github_action(workflow_id)
        with self.assertRaises(SideEffectExecutionError) as caught:
            await github_execute(executor, action, engine)
        self.assertEqual(caught.exception.error_code, "github_permission_denied")

    async def test_aq_5xx_temporary(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.get_status = 500
        action = github_action(workflow_id)
        with self.assertRaises(SideEffectExecutionError) as caught:
            await github_execute(executor, action, engine)
        self.assertEqual(caught.exception.error_code, "github_temporary_error")

    async def test_ar_write_timeout_uncertain(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        fake.hang_mutate = True
        action = github_action(workflow_id)
        context = SideEffectExecutionContext(now=T0, timeout_seconds=0.05)
        result = await github_execute(executor, action, engine, context=context)
        self.assertEqual(result.outcome, OUTCOME_UNCERTAIN)

    async def test_as_post_write_verify_timeout_uncertain(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        fake.hang_verify = True
        action = github_action(workflow_id)
        context = SideEffectExecutionContext(now=T0, timeout_seconds=0.05)
        result = await github_execute(executor, action, engine, context=context)
        self.assertEqual(result.outcome, OUTCOME_UNCERTAIN)

    async def test_at_mutation_success_verify_contradicts(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        fake.contradict_after = True
        action = github_action(workflow_id)
        result = await github_execute(executor, action, engine)
        self.assertEqual(result.outcome, OUTCOME_UNCERTAIN)
        self.assertNotEqual(result.status, STATUS_SUCCEEDED)

    async def test_remove_404_follow_up_get(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        fake.remove_not_found = True
        action = github_action(workflow_id, operation=OP_ENSURE_ABSENT)
        result = await github_execute(executor, action, engine)
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertGreaterEqual(fake.get_calls, 2)

    async def _authorized_rollback(self, engine, executor, result, action, key):
        rollback_action = github_action(
            action.workflow_id,
            idempotency_key=key,
            resource=action.resource,
            operation=action.operation,
        )
        permit = await issue_permit(engine, rollback_action)
        return await executor.rollback(
            result.execution_id,
            action=rollback_action,
            permit=permit,
            context=SideEffectExecutionContext(now=T0),
            gate=engine._gate(),
            hitl=engine._hitl(),
            evaluate_kwargs=github_eval_kwargs(),
        )

    async def test_ah_added_label_rollback_removes(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-ah")
        result = await github_execute(executor, action, engine)
        self.assertIn("bug", fake.current("octo", "hello", 1))
        await self._authorized_rollback(engine, executor, result, action, "gh-ah-rb")
        self.assertNotIn("bug", fake.current("octo", "hello", 1))
        self.assertEqual(executor.store.get(result.execution_id).rollback_status, ROLLBACK_SUCCEEDED)

    async def test_ai_removed_label_rollback_restores(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, ["bug"])
        action = github_action(
            workflow_id, operation=OP_ENSURE_ABSENT, idempotency_key="gh-ai"
        )
        result = await github_execute(executor, action, engine)
        self.assertNotIn("bug", fake.current("octo", "hello", 1))
        await self._authorized_rollback(engine, executor, result, action, "gh-ai-rb")
        self.assertIn("bug", fake.current("octo", "hello", 1))

    async def test_aj_already_present_rollback_noop(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, ["bug"])
        action = github_action(workflow_id, idempotency_key="gh-aj")
        result = await github_execute(executor, action, engine)
        removes = fake.remove_calls
        await self._authorized_rollback(engine, executor, result, action, "gh-aj-rb")
        self.assertEqual(fake.remove_calls, removes)
        self.assertIn("bug", fake.current("octo", "hello", 1))

    async def test_ak_already_absent_rollback_noop(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(
            workflow_id, operation=OP_ENSURE_ABSENT, idempotency_key="gh-ak"
        )
        result = await github_execute(executor, action, engine)
        adds = fake.add_calls
        await self._authorized_rollback(engine, executor, result, action, "gh-ak-rb")
        self.assertEqual(fake.add_calls, adds)
        self.assertNotIn("bug", fake.current("octo", "hello", 1))

    async def test_al_rollback_verifies_final_state(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-al")
        result = await github_execute(executor, action, engine)
        gets = fake.get_calls
        await self._authorized_rollback(engine, executor, result, action, "gh-al-rb")
        self.assertGreater(fake.get_calls, gets)

    async def test_am_rollback_timeout_not_fake_success(self):
        engine, workflow_id, adapter, executor, fake = github_runtime(timeout_seconds=0.05)
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-am")
        result = await github_execute(executor, action, engine)
        fake.hang_mutate = True
        with self.assertRaises(RollbackExecutionError):
            await self._authorized_rollback(engine, executor, result, action, "gh-am-rb")
        record = executor.store.get(result.execution_id)
        self.assertNotEqual(record.rollback_status, ROLLBACK_SUCCEEDED)

    async def test_an_rollback_requires_authorization(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="gh-an")
        result = await github_execute(executor, action, engine)
        rollback_action = github_action(workflow_id, idempotency_key="gh-an-rb")
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.rollback(
                result.execution_id,
                action=rollback_action,
                context=SideEffectExecutionContext(now=T0),
                gate=engine._gate(),
            )
        self.assertIn("bug", fake.current("octo", "hello", 1))

