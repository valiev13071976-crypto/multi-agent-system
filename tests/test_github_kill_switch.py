import unittest

from side_effects.errors import SideEffectActivationDeniedError
from side_effects.github.activation import GitHubWriteActivationService
from side_effects.github.config import GitHubWriteAdapterConfig
from side_effects.models import STATUS_SUCCEEDED, SideEffectExecutionContext
from tests.side_effect_fixtures import (
    T0,
    github_action,
    github_activation_runtime,
    github_eval_kwargs,
    github_execute,
    github_recon_runtime,
    github_runtime,
    issue_permit,
)


class GitHubKillSwitchTests(unittest.IsolatedAsyncioTestCase):

    async def test_g_kill_switch_blocks_mutation(self):
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=False,
            kill_switch=True,
            require_probe_success=False,
        )
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=config
        )
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id)
        with self.assertRaises(SideEffectActivationDeniedError) as caught:
            await github_execute(executor, action, engine)
        self.assertEqual(caught.exception.error_code, "github_write_kill_switch_active")
        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(fake.remove_calls, 0)

    async def test_h_kill_switch_overrides_dry_run_false(self):
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=False,
            kill_switch=True,
            require_probe_success=False,
        )
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=config
        )
        action = github_action(workflow_id)
        with self.assertRaises(SideEffectActivationDeniedError):
            await github_execute(executor, action, engine)
        self.assertEqual(fake.add_calls, 0)

    async def test_i_kill_switch_blocks_rollback(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="ks-write")
        result = await github_execute(executor, action, engine)
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        blocked = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=False,
            kill_switch=True,
            require_probe_success=False,
        )
        executor.activation = GitHubWriteActivationService(
            config=blocked, transport=fake, registered=True
        )
        rollback_action = github_action(workflow_id, idempotency_key="ks-rb")
        permit = await issue_permit(engine, rollback_action)
        removes = fake.remove_calls
        with self.assertRaises(SideEffectActivationDeniedError):
            await executor.rollback(
                result.execution_id,
                action=rollback_action,
                permit=permit,
                context=SideEffectExecutionContext(now=T0),
                gate=engine._gate(),
                hitl=engine._hitl(),
                evaluate_kwargs=github_eval_kwargs(),
            )
        self.assertEqual(fake.remove_calls, removes)

    async def test_j_kill_switch_allows_reconciliation_get(self):
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=False,
            kill_switch=False,
            require_probe_success=False,
        )
        engine, workflow_id, adapter, executor, fake, service = github_recon_runtime(
            config=config
        )
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="ks-recon")
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
        executor.activation = GitHubWriteActivationService(
            config=GitHubWriteAdapterConfig(
                enabled=True,
                allowed_repositories=("octo/hello",),
                dry_run=False,
                kill_switch=True,
                require_probe_success=False,
            ),
            transport=fake,
            registered=True,
        )
        gets = fake.get_calls
        record = service.store.find_by_execution(result.execution_id)[0]
        await service.reconcile(record.reconciliation_id, action=action, now=T0)
        self.assertGreater(fake.get_calls, gets)
        self.assertEqual(fake.add_calls, 1)

    async def test_k_kill_switch_allows_readiness_probe(self):
        fake_runtime = github_activation_runtime(
            config=GitHubWriteAdapterConfig(
                enabled=True,
                allowed_repositories=("octo/hello",),
                dry_run=False,
                kill_switch=True,
                require_probe_success=False,
            )
        )
        engine, workflow_id, adapter, executor, fake, activation = fake_runtime
        fake.seed_repository("octo", "hello")
        result = await activation.refresh()
        self.assertEqual(fake.get_repository_calls, 1)
        self.assertEqual(fake.add_calls, 0)
        self.assertTrue(result.repository_accessible)
