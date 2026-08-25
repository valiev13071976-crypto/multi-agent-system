from datetime import timedelta
import unittest

from fastapi.testclient import TestClient

from autonomy.models import DECISION_ALLOW, DECISION_DENY, DECISION_REQUIRE_APPROVAL
from hitl.errors import ActionIntegrityError
from side_effects.errors import (
    SideEffectAdapterNotFoundError,
    SideEffectAuthorizationError,
    SideEffectExecutionDeniedError,
    SideEffectExecutionError,
    SideEffectIdempotencyError,
)
from side_effects.executor import SideEffectExecutor
from side_effects.models import (
    OUTCOME_KNOWN_SUCCESS,
    OUTCOME_UNCERTAIN,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    SideEffectExecutionRequest,
    SideEffectExecutionResult,
)
from side_effects.registry import empty_adapter_registry
from tests.side_effect_fixtures import (
    T0,
    allow_execute,
    ctx,
    eval_kwargs,
    hitl_runtime,
    issue_permit,
    runtime,
    se_action,
)
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from workflow.models import STATUS_FAILED, STATUS_WAITING_APPROVAL


class SideEffectExecutorTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_no_adapter_registered_denies(self):
        engine, workflow_id, _, _ = runtime()
        engine.side_effect_executor = SideEffectExecutor(
            empty_adapter_registry(), gate=engine._gate()
        )
        action = se_action(workflow_id)
        with self.assertRaises(SideEffectAdapterNotFoundError):
            await allow_execute(engine.side_effect_executor, action, engine)

    async def test_b_bounded_allow_succeeds(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id)
        result = await allow_execute(executor, action, engine, "hello")
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(result.outcome, OUTCOME_KNOWN_SUCCESS)
        self.assertEqual(adapter.data.get("test/key"), "hello")

    async def test_c_adapter_called_once(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id)
        await allow_execute(executor, action, engine)
        self.assertEqual(adapter.calls, 1)

    async def test_d_execution_result_normalized(self):
        engine, workflow_id, _, executor = runtime()
        action = se_action(workflow_id)
        result = await allow_execute(executor, action, engine)
        self.assertIsInstance(result, SideEffectExecutionResult)
        self.assertTrue(result.external_reference)
        self.assertNotEqual(result.status, "done")
        self.assertIn(result.status, {"succeeded", "failed", "cancelled", "unknown", "denied"})

    async def test_e_ids_preserved(self):
        engine, workflow_id, _, executor = runtime()
        action = se_action(workflow_id, task_id="task-se")
        result = await allow_execute(executor, action, engine)
        self.assertEqual(result.workflow_id, workflow_id)
        self.assertEqual(result.task_id, "task-se")
        self.assertEqual(result.action_id, action.action_id)
        self.assertEqual(result.tool_id, action.tool_id)
        self.assertEqual(result.operation, action.operation)

    def test_f_public_analyze_unchanged(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json().keys()), set(CONTRACT_KEYS))

    async def test_g_missing_authorization_no_adapter(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id)
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.execute(
                action,
                context=ctx(),
                gate=engine._gate(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs(),
            )
        self.assertEqual(adapter.calls, 0)

    async def test_h_deny_decision_no_adapter(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id)
        decision = engine._gate().evaluate(action, capabilities=eval_kwargs()["capabilities"])
        self.assertEqual(decision.decision, DECISION_DENY)
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.execute(
                action,
                decision=decision,
                context=ctx(),
                gate=engine._gate(),
                state_manager=engine.state_manager,
                evaluate_kwargs={"capabilities": eval_kwargs()["capabilities"], "now": T0},
            )
        self.assertEqual(adapter.calls, 0)

    async def test_i_require_approval_without_permit(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        decision = engine._gate().evaluate(action, **eval_kwargs("executor_confirmed"))
        self.assertEqual(decision.decision, DECISION_REQUIRE_APPROVAL)
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.execute(
                action,
                decision=decision,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(adapter.calls, 0)

    async def test_j_valid_permit_allows_execution(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        result = await executor.execute(
            action,
            permit=permit,
            context=ctx("via-permit"),
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs("executor_confirmed"),
        )
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(adapter.calls, 1)

    async def test_k_expired_permit_no_adapter(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.execute(
                action,
                permit=permit,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                now=T0 + timedelta(seconds=301),
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(adapter.calls, 0)

    async def test_l_consumed_permit_no_adapter(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        engine._hitl().consume_for_execution(permit.permit_id, action=action, now=T0)
        consumed = engine._hitl().permits.get(permit.permit_id)
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.execute(
                action,
                permit=consumed,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(adapter.calls, 0)

    async def test_m_wrong_fingerprint_no_adapter(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        other = se_action(
            workflow_id,
            action_id=action.action_id,
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            resource="test/other",
            idempotency_key=action.idempotency_key,
        )
        with self.assertRaises((SideEffectAuthorizationError, ActionIntegrityError)):
            await executor.execute(
                other,
                permit=permit,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(adapter.calls, 0)

    async def test_aa_idempotency_failure_does_not_consume_permit(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        broken = se_action(
            workflow_id,
            action_id=action.action_id,
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            idempotency_key=None,
        )
        with self.assertRaises(SideEffectIdempotencyError):
            await executor.execute(
                broken,
                permit=permit,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
            )
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(engine._hitl().permits.get(permit.permit_id).status, "issued")

    async def test_ab_permit_consume_failure_no_adapter(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)

        class BoomHITL:
            def __init__(self, real):
                self._real = real
                self.permits = real.permits

            def consume_for_execution(self, *args, **kwargs):
                raise RuntimeError("consume_boom")

            def __getattr__(self, name):
                return getattr(self._real, name)

        with self.assertRaises(SideEffectExecutionDeniedError):
            await executor.execute(
                action,
                permit=permit,
                context=ctx(),
                gate=engine._gate(),
                hitl=BoomHITL(engine._hitl()),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(adapter.calls, 0)

    async def test_ac_permit_consumed_immediately_before_adapter(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        await executor.execute(
            action,
            permit=permit,
            context=ctx(),
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs("executor_confirmed"),
        )
        self.assertEqual(executor.trace, ["permit_consumed", "adapter_started"])

    async def test_ad_adapter_failure_keeps_permit_consumed(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        adapter.fail_before_write = True
        with self.assertRaises(SideEffectExecutionError):
            await executor.execute(
                action,
                permit=permit,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(engine._hitl().permits.get(permit.permit_id).status, "consumed")

    async def test_ae_af_ag_ah_uncertain_outcome(self):
        engine2, workflow_id2, adapter2, executor2 = runtime()
        action2 = se_action(workflow_id2, idempotency_key="idem-uncertain")
        kwargs2 = eval_kwargs()
        decision2 = engine2._gate().evaluate(action2, **kwargs2)
        context = ctx("mutated")
        context.simulate_finalization_failure = True
        result2 = await executor2.execute(
            action2,
            decision=decision2,
            context=context,
            gate=engine2._gate(),
            state_manager=engine2.state_manager,
            evaluate_kwargs=kwargs2,
        )
        self.assertEqual(result2.status, STATUS_UNKNOWN)
        self.assertEqual(result2.outcome, OUTCOME_UNCERTAIN)
        self.assertEqual(adapter2.data.get("test/key"), "mutated")
        with self.assertRaises(SideEffectIdempotencyError):
            await executor2.execute(
                action2,
                decision=decision2,
                context=ctx("again"),
                gate=engine2._gate(),
                state_manager=engine2.state_manager,
                evaluate_kwargs=kwargs2,
            )
        self.assertEqual(adapter2.calls, 1)
        self.assertEqual(
            engine2.state_manager.get(workflow_id2).status, STATUS_FAILED
        )
        self.assertEqual(
            engine2.state_manager.get(workflow_id2).error_code,
            "execution_outcome_uncertain",
        )
        self.assertEqual(adapter2.rollback_calls, 0)

    async def test_bh_waiting_approval_cannot_execute(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        decision = engine.evaluate_action(
            action, requested_by="agent-1", **eval_kwargs("executor_confirmed")
        )
        self.assertEqual(
            engine.state_manager.get(workflow_id).status, STATUS_WAITING_APPROVAL
        )
        with self.assertRaises(SideEffectExecutionDeniedError):
            await executor.execute(
                action,
                decision=decision,
                context=ctx(),
                gate=engine._gate(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs("executor_confirmed"),
            )
        self.assertEqual(adapter.calls, 0)

    async def test_bi_terminal_cannot_execute(self):
        engine, workflow_id, adapter, executor = runtime()
        engine.state_manager.complete_workflow(workflow_id)
        action = se_action(workflow_id)
        with self.assertRaises(SideEffectExecutionDeniedError):
            await allow_execute(executor, action, engine)
        self.assertEqual(adapter.calls, 0)

    async def test_bj_known_failure_fails_workflow(self):
        engine, workflow_id, adapter, executor = runtime()
        adapter.fail_before_write = True
        action = se_action(workflow_id)
        with self.assertRaises(SideEffectExecutionError):
            await allow_execute(executor, action, engine)
        self.assertEqual(engine.state_manager.get(workflow_id).status, STATUS_FAILED)

    def test_request_model_fields(self):
        req = SideEffectExecutionRequest(
            execution_id="e1",
            workflow_id="w1",
            task_id="t1",
            action_id="a1",
            tool_id="test.reversible_store",
            operation="set_value",
            resource="test/key",
            action_fingerprint="abc",
            idempotency_key="k",
            authorization_type="autonomy_decision",
            authorization_id="d1",
            requested_at=T0,
            metadata={"prompt": "secret-prompt", "tool_id": "test.reversible_store"},
        )
        self.assertNotIn("secret-prompt", str(dict(req.metadata)))


if __name__ == "__main__":
    unittest.main()
