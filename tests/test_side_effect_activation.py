import unittest

from side_effects.activation import (
    ACTIVATION_BLOCKED,
    ACTIVATION_DISABLED,
    ACTIVATION_DRY_RUN,
    OperationalActivationDecision,
)
from side_effects.github.activation import GitHubWriteActivationService
from side_effects.github.config import GitHubWriteAdapterConfig
from side_effects.github.models import GITHUB_TOOL_ID
from side_effects.github.transport import FakeGitHubTransport
from tests.side_effect_fixtures import github_action, github_activation_runtime, T0


class SideEffectActivationTests(unittest.TestCase):

    def test_disabled_state(self):
        config = GitHubWriteAdapterConfig()
        service = GitHubWriteActivationService(config=config, registered=False)
        self.assertEqual(service.state, ACTIVATION_DISABLED)
        health = service.health()
        self.assertEqual(health.adapter_id, GITHUB_TOOL_ID)
        self.assertEqual(health.activation_state, ACTIVATION_DISABLED)
        self.assertTrue(health.kill_switch)
        self.assertTrue(health.dry_run)

    def test_kill_switch_state_blocked(self):
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            kill_switch=True,
            dry_run=False,
        )
        service = GitHubWriteActivationService(config=config, registered=True)
        self.assertEqual(service.state, ACTIVATION_BLOCKED)

    def test_dry_run_state(self):
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            kill_switch=False,
            dry_run=True,
            require_probe_success=False,
        )
        service = GitHubWriteActivationService(config=config, registered=True)
        self.assertEqual(service.state, ACTIVATION_DRY_RUN)


class SideEffectActivationEvaluateTests(unittest.IsolatedAsyncioTestCase):

    async def test_al_permit_valid_activation_blocked(self):
        from side_effects.errors import SideEffectActivationDeniedError
        from tests.side_effect_fixtures import github_eval_kwargs, github_execute, issue_permit
        from side_effects.models import SideEffectExecutionContext
        from tests.side_effect_fixtures import T0

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
        with self.assertRaises(SideEffectActivationDeniedError) as caught:
            await github_execute(executor, action, engine)
        self.assertEqual(caught.exception.error_code, "github_write_kill_switch_active")
        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(adapter.calls, 0)

    async def test_am_ready_invalid_permit(self):
        from side_effects.errors import SideEffectAuthorizationError
        from tests.side_effect_fixtures import github_eval_kwargs, issue_permit
        from side_effects.models import SideEffectExecutionContext
        from tests.side_effect_fixtures import T0

        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=False,
            kill_switch=False,
            require_probe_success=False,
        )
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=config
        )
        action = github_action(workflow_id)
        with self.assertRaises(SideEffectAuthorizationError):
            await executor.execute(
                action,
                context=SideEffectExecutionContext(now=T0),
                gate=engine._gate(),
                state_manager=engine.state_manager,
            )
        self.assertEqual(fake.add_calls, 0)

    async def test_an_ready_valid_permit_mutates_fake(self):
        from tests.side_effect_fixtures import github_execute
        from side_effects.models import STATUS_SUCCEEDED

        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=False,
            kill_switch=False,
            require_probe_success=True,
        )
        engine, workflow_id, adapter, executor, fake, service = github_activation_runtime(
            config=config
        )
        fake.seed_repository("octo", "hello")
        fake.seed("octo", "hello", 1, [])
        await service.refresh(now=T0)
        action = github_action(workflow_id)
        result = await github_execute(executor, action, engine)
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(fake.add_calls, 1)
