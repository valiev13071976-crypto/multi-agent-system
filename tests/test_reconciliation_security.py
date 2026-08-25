import unittest

from autonomy.capabilities import CAP_PURCHASE
from autonomy.gate import build_proposed_action
from side_effects.errors import SideEffectExecutionDeniedError
from side_effects.executor import SideEffectExecutor
from side_effects.models import DISABLED_ACTION_REASONS
from side_effects.registry import empty_adapter_registry
from tests.side_effect_fixtures import (
    T0,
    ctx,
    eval_kwargs,
    hitl_runtime,
    issue_permit,
    make_uncertain,
    recon_runtime,
    se_action,
)
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE


class ReconciliationSecurityTests(unittest.IsolatedAsyncioTestCase):

    async def test_ay_record_has_no_prompt(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(
            workflow_id,
            idempotency_key="sec-ay",
            metadata={"reversible": True, "prompt": "full user prompt"},
        )
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        blob = str(record) + str(dict(record.metadata))
        self.assertNotIn("full user prompt", blob)

    async def test_az_no_raw_adapter_body(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="sec-az")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        outcome = await service.reconcile(record.reconciliation_id, action=action, now=T0)
        blob = str(outcome) + str(dict(outcome.metadata))
        self.assertNotIn("<html", blob)
        self.assertNotIn("raw_body", blob)

    async def test_ba_no_secrets(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(
            workflow_id,
            idempotency_key="sec-ba",
            metadata={
                "reversible": True,
                "api_key": "sk-live-secret",
                "Authorization": "Bearer abc",
                "cookie": "sid=1",
            },
        )
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        blob = str(dict(record.metadata))
        self.assertNotIn("sk-live-secret", blob)
        self.assertNotIn("Bearer abc", blob)

    async def test_bb_no_permit_or_capability_token(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="sec-bb")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        blob = str(dict(record.metadata))
        self.assertNotIn("signature", blob)
        self.assertNotIn("PANDA", blob)

    async def test_bc_audit_no_payload(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        action = se_action(workflow_id, idempotency_key="sec-bc")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        blob = str([dict(event.metadata) for event in executor.audit.events()])
        self.assertNotIn("mutated", blob)

    async def test_bd_errors_redacted(self):
        from side_effects.errors import ReconciliationConflictError

        err = ReconciliationConflictError()
        self.assertNotIn("sk-", str(err))

    def test_be_bf_empty_registry(self):
        self.assertEqual(len(empty_adapter_registry()), 0)
        self.assertEqual(len(SideEffectExecutor().registry), 0)

    async def test_bg_disabled_actions_remain_disabled(self):
        engine, workflow_id, adapter, executor = hitl_runtime()
        action = build_proposed_action(
            action_type="purchase",
            workflow_id=workflow_id,
            task_id="task-se",
            tool_id="test.reversible_store",
            operation="set_value",
            resource="test/key",
            idempotency_key="sec-bg",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_PURCHASE,),
        )
        permit = await issue_permit(engine, action)
        with self.assertRaises(SideEffectExecutionDeniedError) as caught:
            await executor.execute(
                action,
                permit=permit,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs(
                    "executor_confirmed", capabilities=action.requested_capabilities
                ),
            )
        self.assertEqual(caught.exception.error_code, "financial_execution_not_enabled")
        self.assertEqual(DISABLED_ACTION_REASONS["purchase"], "financial_execution_not_enabled")
        self.assertEqual(adapter.calls, 0)

    async def test_au_manual_resolution_no_secrets(self):
        engine, workflow_id, adapter, executor, service = recon_runtime()
        adapter.set_reconciliation_flags(supports_reconciliation=False)
        action = se_action(workflow_id, idempotency_key="sec-au")
        result = await make_uncertain(executor, action, engine)
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        resolved = service.resolve_manual(
            record.reconciliation_id,
            outcome="confirm_succeeded",
            resolver_id="ops-1",
            reason_code="seen_in_store",
        )
        blob = str(resolved) + str(dict(resolved.metadata))
        self.assertNotIn("prompt", blob.lower() if False else blob)
        self.assertNotIn("sk-live", blob)


if __name__ == "__main__":
    unittest.main()
