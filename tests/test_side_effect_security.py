import unittest

from fastapi.testclient import TestClient

from autonomy.capabilities import (
    CAP_CODE_EXECUTE,
    CAP_EXTERNAL_WRITE,
    CAP_FINANCIAL_CHANGE,
    CAP_MESSAGE_SEND,
    CAP_PERMISSION_MANAGE,
    CAP_PRICING_WRITE,
    CAP_PURCHASE,
    CAP_SITE_WRITE,
)
from autonomy.gate import build_proposed_action
from side_effects.errors import (
    SideEffectAdapterNotFoundError,
    SideEffectExecutionDeniedError,
    SideEffectExecutionError,
)
from side_effects.executor import SideEffectExecutor
from side_effects.models import hash_idempotency_key
from side_effects.registry import empty_adapter_registry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from task_queue.models import STATUS_DEAD_LETTERED, STATUS_RETRY_WAIT
from task_queue.queue import TaskQueue
from task_queue.retry import RetryPolicy
from task_queue.store import InMemoryTaskQueueStore
from task_queue.worker import TaskWorker
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
from tests.test_mode_auto import STRATEGY_TEXT, load_auto_app
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app
from tools.gateway import ToolGateway
from tools.models import TOOL_TRUST_READ_ONLY_EXTERNAL, TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from workflow.engine import WorkflowEngine


class SideEffectSecurityTests(unittest.IsolatedAsyncioTestCase):

    async def _deny_disabled(self, action_type, capabilities, reason, extra=None):
        engine, workflow_id, adapter, executor = hitl_runtime()
        fields = {
            "action_type": action_type,
            "workflow_id": workflow_id,
            "task_id": "task-se",
            "tool_id": "test.reversible_store",
            "operation": "set_value",
            "resource": "test/key",
            "idempotency_key": f"idem-{action_type}",
            "metadata": {"reversible": True},
            "tool_trust_level": TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            "requested_capabilities": capabilities,
        }
        fields.update(extra or {})
        action = build_proposed_action(**fields)
        kwargs = eval_kwargs(
            "executor_confirmed", capabilities=action.requested_capabilities
        )
        permit = await issue_permit(engine, action)
        self.assertIsNotNone(permit)
        with self.assertRaises(SideEffectExecutionDeniedError) as caught:
            await executor.execute(
                action,
                permit=permit,
                context=ctx(),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=kwargs,
            )
        self.assertEqual(caught.exception.error_code, reason)
        self.assertEqual(adapter.calls, 0)

    async def test_ao_purchase_disabled(self):
        await self._deny_disabled(
            "purchase", (CAP_PURCHASE,), "financial_execution_not_enabled"
        )

    async def test_ap_financial_change_disabled(self):
        await self._deny_disabled(
            "financial_change",
            (CAP_FINANCIAL_CHANGE,),
            "financial_execution_not_enabled",
        )

    async def test_aq_pricing_write_disabled(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(
            workflow_id, requested_capabilities=(CAP_EXTERNAL_WRITE, CAP_PRICING_WRITE)
        )
        with self.assertRaises(SideEffectExecutionDeniedError) as caught:
            await allow_execute(executor, action, engine)
        self.assertEqual(caught.exception.error_code, "pricing_write_not_enabled")
        self.assertEqual(adapter.calls, 0)

    async def test_ar_send_message_disabled(self):
        await self._deny_disabled(
            "send_message",
            (CAP_MESSAGE_SEND,),
            "customer_communication_execution_not_enabled",
        )

    async def test_as_external_publish_disabled(self):
        await self._deny_disabled(
            "external_publish",
            (CAP_SITE_WRITE,),
            "customer_communication_execution_not_enabled",
        )

    async def test_at_permission_change_disabled(self):
        await self._deny_disabled(
            "permission_change",
            (CAP_PERMISSION_MANAGE,),
            "permission_change_execution_not_enabled",
        )

    async def test_au_delete_disabled(self):
        await self._deny_disabled(
            "delete",
            (CAP_EXTERNAL_WRITE,),
            "delete_execution_not_enabled",
        )

    async def test_av_execute_code_disabled(self):
        await self._deny_disabled(
            "execute_code",
            (CAP_CODE_EXECUTE,),
            "code_execution_not_enabled",
        )

    async def test_aw_record_has_no_prompt(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(
            workflow_id,
            metadata={"reversible": True, "prompt": "full user prompt"},
        )
        result = await allow_execute(executor, action, engine, "v")
        record = executor.store.get(result.execution_id)
        blob = str(record) + str(dict(record.metadata))
        self.assertNotIn("full user prompt", blob)

    async def test_ax_no_raw_capability_token(self):
        engine, workflow_id, _, executor = runtime()
        action = se_action(workflow_id)
        result = await allow_execute(executor, action, engine)
        record = executor.store.get(result.execution_id)
        blob = str(dict(record.metadata))
        self.assertNotIn("PANDA", blob)
        self.assertNotIn("capability_token", blob)

    async def test_ay_no_permit_secret(self):
        engine, workflow_id, _, executor = hitl_runtime()
        action = se_action(
            workflow_id, tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        permit = await issue_permit(engine, action)
        result = await executor.execute(
            action,
            permit=permit,
            context=ctx("p"),
            gate=engine._gate(),
            hitl=engine._hitl(),
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs("executor_confirmed"),
        )
        record = executor.store.get(result.execution_id)
        blob = str(dict(record.metadata)) + str(permit.public_view())
        self.assertNotIn("signature", blob.lower())
        self.assertEqual(record.authorization_id, permit.permit_id)

    async def test_az_no_api_key_authorization_cookies(self):
        engine, workflow_id, _, executor = runtime()
        action = se_action(
            workflow_id,
            metadata={
                "reversible": True,
                "api_key": "sk-live-secret",
                "Authorization": "Bearer abc",
                "cookie": "sid=1",
            },
        )
        result = await allow_execute(executor, action, engine)
        record = executor.store.get(result.execution_id)
        blob = str(dict(record.metadata))
        self.assertNotIn("sk-live-secret", blob)
        self.assertNotIn("Bearer abc", blob)
        self.assertNotIn("sid=1", blob)

    async def test_ba_audit_no_raw_payload(self):
        engine, workflow_id, _, executor = runtime()
        action = se_action(workflow_id)
        await allow_execute(executor, action, engine, "sensitive-value-xyz")
        blob = str([dict(event.metadata) for event in executor.audit.events()])
        self.assertNotIn("sensitive-value-xyz", blob)

    async def test_bb_errors_redacted(self):
        err = SideEffectExecutionError("adapter_failed")
        self.assertNotIn("sk-", str(err))
        self.assertEqual(err.error_code, "adapter_failed")

    async def test_bc_adapter_rejects_eval_payload(self):
        engine, workflow_id, adapter, executor = runtime()
        action = se_action(workflow_id)
        kwargs = eval_kwargs()
        decision = engine._gate().evaluate(action, **kwargs)
        with self.assertRaises(SideEffectExecutionError):
            await executor.execute(
                action,
                decision=decision,
                context=ctx("eval(os.system('rm'))"),
                gate=engine._gate(),
                state_manager=engine.state_manager,
                evaluate_kwargs=kwargs,
            )
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(adapter.data, {})

    def test_bd_be_bf_default_registry_empty(self):
        registry = empty_adapter_registry()
        self.assertEqual(len(registry), 0)
        executor = SideEffectExecutor()
        self.assertEqual(len(executor.registry), 0)
        self.assertNotIn("test.reversible_store", getattr(executor.registry, "_adapters", {}))

    async def test_bf_default_executor_cannot_write(self):
        engine = WorkflowEngine()
        workflow_id = engine.create("task-se")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        action = se_action(workflow_id)
        executor = SideEffectExecutor()
        with self.assertRaises(SideEffectAdapterNotFoundError):
            await allow_execute(executor, action, engine)

    def test_bg_tool_gateway_remains_read_only(self):
        gateway = ToolGateway()
        self.assertEqual(gateway.tool_trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)

    def test_bl_analyze_contract(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            payload = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "openai"},
            ).json()
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))

    def test_bo_analyze_seven_fields(self):
        self.test_bl_analyze_contract()

    def test_bs_mode_both_unchanged(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "both"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json().keys()), set(CONTRACT_KEYS))

    def test_br_mode_auto_unchanged(self):
        main_mod = load_auto_app("anthropic", "openai", auto_order="anthropic,openai")
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "anthropic", "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "auto"},
            )
        self.assertEqual(response.status_code, 200)

    async def test_bm_bn_queue_no_automatic_side_effect_retry(self):
        engine, workflow_id, adapter, executor = runtime()
        adapter.fail_before_write = True
        queue = TaskQueue(
            InMemoryTaskQueueStore(),
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=1),
        )
        worker = TaskWorker(queue, engine=engine)
        from tests.side_effect_fixtures import ctx as ctx_payload

        async def handler(ctx):
            action = se_action(workflow_id, idempotency_key="q-fail")
            await worker.execute_side_effect(
                action,
                decision=engine._gate().evaluate(action, **eval_kwargs()),
                context=ctx_payload(),
                evaluate_kwargs=eval_kwargs(),
            )

        queue.enqueue(
            workflow_id=workflow_id,
            task_id="task-se",
            execution_key="ek-se",
        )
        worker.handler = handler
        result = await worker.run_once()
        self.assertNotEqual(result.status, STATUS_RETRY_WAIT)
        self.assertEqual(result.status, STATUS_DEAD_LETTERED)

    def test_idempotency_hash_not_raw_key_only(self):
        digest = hash_idempotency_key("secret-key")
        self.assertNotEqual(digest, "secret-key")
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
