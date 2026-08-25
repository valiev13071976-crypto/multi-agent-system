import unittest

from hitl.models import PERMIT_CONSUMED, PERMIT_ISSUED
from side_effects.errors import SideEffectActivationDeniedError
from side_effects.github.config import GitHubWriteAdapterConfig
from side_effects.github.models import OP_ENSURE_ABSENT, OP_ENSURE_PRESENT
from side_effects.models import (
    EVENT_ADAPTER_SUCCEEDED,
    EVENT_DRY_RUN_COMPLETED,
    EVENT_PERMIT_CONSUMED,
    SideEffectExecutionContext,
)
from tests.side_effect_fixtures import (
    T0,
    github_action,
    github_activation_runtime,
    github_eval_kwargs,
    github_execute,
    issue_permit,
)


def _dry_config(**extra):
    fields = dict(
        enabled=True,
        allowed_repositories=("octo/hello",),
        dry_run=True,
        kill_switch=False,
        require_probe_success=False,
    )
    fields.update(extra)
    return GitHubWriteAdapterConfig(**fields)


class GitHubDryRunTests(unittest.IsolatedAsyncioTestCase):

    async def test_l_ensure_present_would_change(self):
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=_dry_config()
        )
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, operation=OP_ENSURE_PRESENT)
        result = await executor.dry_run(
            action,
            context=SideEffectExecutionContext(now=T0),
            gate=engine._gate(),
            evaluate_kwargs=github_eval_kwargs(),
        )
        self.assertTrue(result.would_change)
        self.assertTrue(result.current_state_known)
        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(fake.remove_calls, 0)

    async def test_m_already_present_no_change(self):
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=_dry_config()
        )
        fake.seed("octo", "hello", 1, ["bug"])
        action = github_action(workflow_id)
        result = await executor.dry_run(
            action,
            context=SideEffectExecutionContext(now=T0),
            gate=engine._gate(),
            evaluate_kwargs=github_eval_kwargs(),
        )
        self.assertFalse(result.would_change)
        self.assertEqual(fake.add_calls, 0)

    async def test_n_ensure_absent_would_remove(self):
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=_dry_config()
        )
        fake.seed("octo", "hello", 1, ["bug"])
        action = github_action(workflow_id, operation=OP_ENSURE_ABSENT)
        result = await executor.dry_run(
            action,
            context=SideEffectExecutionContext(now=T0),
            gate=engine._gate(),
            evaluate_kwargs=github_eval_kwargs(),
        )
        self.assertTrue(result.would_change)
        self.assertEqual(fake.remove_calls, 0)

    async def test_o_does_not_consume_permit(self):
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=_dry_config()
        )
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id)
        permit = await issue_permit(engine, action)
        await executor.dry_run(
            action,
            context=SideEffectExecutionContext(now=T0),
            gate=engine._gate(),
            evaluate_kwargs=github_eval_kwargs(),
        )
        stored = engine._hitl().permits.get(permit.permit_id)
        self.assertEqual(stored.status, PERMIT_ISSUED)
        self.assertNotEqual(stored.status, PERMIT_CONSUMED)
        types = [event.event_type for event in executor.audit.events()]
        self.assertNotIn(EVENT_PERMIT_CONSUMED, types)

    async def test_p_does_not_modify_idempotency(self):
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=_dry_config()
        )
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="dry-key-x")
        await executor.dry_run(
            action,
            context=SideEffectExecutionContext(now=T0),
            gate=engine._gate(),
            evaluate_kwargs=github_eval_kwargs(),
        )
        record = engine._gate().idempotency.get("dry-key-x")
        self.assertIsNone(record)

    async def test_q_audit_distinct_from_real(self):
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=_dry_config()
        )
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id)
        await executor.dry_run(
            action,
            context=SideEffectExecutionContext(now=T0),
            gate=engine._gate(),
            evaluate_kwargs=github_eval_kwargs(),
        )
        types = [event.event_type for event in executor.audit.events()]
        self.assertIn(EVENT_DRY_RUN_COMPLETED, types)
        self.assertNotIn(EVENT_ADAPTER_SUCCEEDED, types)

    async def test_execute_in_dry_run_does_not_mutate(self):
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=_dry_config()
        )
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id)
        with self.assertRaises(SideEffectActivationDeniedError) as caught:
            await github_execute(executor, action, engine)
        self.assertEqual(caught.exception.error_code, "github_write_dry_run_active")
        self.assertEqual(fake.add_calls, 0)
