"""Recovery persistence failure fails closed for mutation."""

from __future__ import annotations

import unittest

from recovery.orchestrator import RecoveryMutationBlocked, RecoveryOrchestrator
from recovery.store import InMemoryRecoveryCaseStore, RecoveryPersistenceUnavailableError
from side_effects.errors import SideEffectPersistenceUnavailableError
from side_effects.executor import SideEffectExecutor
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import se_action, ctx, eval_kwargs
from tools.models import TOOL_TRUST_INTERNAL_SAFE
from workflow.engine import WorkflowEngine


class RecoveryPersistenceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_mutation_blocked_when_recovery_unavailable(self):
        store = InMemoryRecoveryCaseStore()
        orch = RecoveryOrchestrator(store=store, enqueue_reconcile_on_create=False)
        orch._fail_closed_persistence()
        with self.assertRaises(RecoveryMutationBlocked):
            orch.require_mutation_allowed()

        engine = WorkflowEngine()
        wf = engine.create("t", tenant_id="tenant-se")
        engine.state_manager.plan(wf)
        engine.state_manager.start(wf)
        adapter = InMemoryReversibleWriteAdapter(trust_level=TOOL_TRUST_INTERNAL_SAFE)
        registry = SideEffectAdapterRegistry()
        registry.register(adapter)
        executor = SideEffectExecutor(registry, gate=engine._gate())
        executor.recovery_orchestrator = orch
        action = se_action(wf, idempotency_key="fail-closed")
        decision = engine._gate().evaluate(action, **eval_kwargs())
        with self.assertRaises(SideEffectPersistenceUnavailableError):
            await executor.execute(
                action,
                decision=decision,
                context=ctx("v"),
                gate=engine._gate(),
                state_manager=engine.state_manager,
                evaluate_kwargs=eval_kwargs(),
            )
        self.assertEqual(adapter.calls, 0)

    def test_create_marks_unavailable(self):
        store = InMemoryRecoveryCaseStore()
        orch = RecoveryOrchestrator(store=store, enqueue_reconcile_on_create=False)
        store.available = False
        with self.assertRaises(RecoveryPersistenceUnavailableError):
            orch.create_case(
                execution_id="e",
                case_type="uncertain_side_effect",
                enqueue=False,
            )
        self.assertEqual(orch.mutation_blocked_reason, "recovery_persistence_unavailable")


if __name__ == "__main__":
    unittest.main()
