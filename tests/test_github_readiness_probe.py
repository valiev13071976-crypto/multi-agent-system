from datetime import timedelta
import unittest

from tests.side_effect_fixtures import T0
from side_effects.activation import (
    READINESS_BLOCKED,
    READINESS_PARTIAL,
    READINESS_READY,
    READINESS_UNKNOWN,
    WRITE_PERMISSION_CONFIRMED,
    WRITE_PERMISSION_UNCONFIRMED,
)
from side_effects.github.config import GitHubWriteAdapterConfig
from side_effects.github.readiness import GitHubReadinessProbe
from side_effects.github.transport import FakeGitHubTransport
from side_effects.github.activation import GitHubWriteActivationService


class GitHubReadinessProbeTests(unittest.IsolatedAsyncioTestCase):

    async def test_r_valid_repo_accessible(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello")
        probe = GitHubReadinessProbe(fake)
        result = await probe.probe(("octo/hello",), ttl_seconds=300, now=T0)
        self.assertEqual(result.status, READINESS_READY)
        self.assertTrue(result.authenticated)
        self.assertTrue(result.repository_accessible)
        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(fake.remove_calls, 0)

    async def test_s_write_permission_unconfirmed(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello")
        result = await GitHubReadinessProbe(fake).probe(("octo/hello",), ttl_seconds=None)
        self.assertEqual(result.write_permission_status, WRITE_PERMISSION_UNCONFIRMED)
        self.assertNotEqual(result.write_permission_status, WRITE_PERMISSION_CONFIRMED)

    async def test_t_401_blocked(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello", status=401)
        result = await GitHubReadinessProbe(fake).probe(("octo/hello",), ttl_seconds=None)
        self.assertEqual(result.status, READINESS_BLOCKED)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason_code, "github_authentication_failed")

    async def test_u_403_blocked(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello", status=403)
        result = await GitHubReadinessProbe(fake).probe(("octo/hello",), ttl_seconds=None)
        self.assertEqual(result.status, READINESS_BLOCKED)

    async def test_v_404_blocked(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello", status=404)
        result = await GitHubReadinessProbe(fake).probe(("octo/hello",), ttl_seconds=None)
        self.assertEqual(result.status, READINESS_BLOCKED)

    async def test_w_rate_limit_no_retry_loop(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello", status=429)
        result = await GitHubReadinessProbe(fake).probe(("octo/hello",), ttl_seconds=None)
        self.assertEqual(result.status, READINESS_BLOCKED)
        self.assertEqual(result.reason_code, "github_probe_rate_limited")
        self.assertEqual(fake.get_repository_calls, 1)

    async def test_x_probe_timeout_unknown(self):
        fake = FakeGitHubTransport()
        fake.hang_probe = True
        fake.seed_repository("octo", "hello")
        from side_effects.github.errors import GitHubAdapterError

        async def boom(*args, **kwargs):
            raise GitHubAdapterError("github_timeout_uncertain")

        fake.get_repository = boom
        result = await GitHubReadinessProbe(fake).probe(("octo/hello",), ttl_seconds=None)
        self.assertEqual(result.status, READINESS_UNKNOWN)
        self.assertEqual(result.reason_code, "github_probe_timeout")

    async def test_y_probe_never_mutates(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello")
        await GitHubReadinessProbe(fake).probe(("octo/hello",), ttl_seconds=None)
        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(fake.remove_calls, 0)

    async def test_ad_partial_readiness(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello", 200)
        fake.seed_repository("octo", "other", 404)
        result = await GitHubReadinessProbe(fake).probe(
            ("octo/hello", "octo/other"), ttl_seconds=None
        )
        self.assertEqual(result.status, READINESS_PARTIAL)

    async def test_z_aa_ab_ttl(self):
        from side_effects.activation import PURPOSE_MUTATE
        from tests.side_effect_fixtures import github_action
        from workflow.engine import WorkflowEngine

        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello")
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello",),
            dry_run=False,
            kill_switch=False,
            require_probe_success=True,
            readiness_ttl_seconds=60,
        )
        service = GitHubWriteActivationService(
            config=config, transport=fake, registered=True
        )
        await service.refresh(now=T0)
        engine = WorkflowEngine()
        workflow_id = engine.create("task-se")
        action = github_action(workflow_id)
        ok = service.evaluate(action, None, purpose=PURPOSE_MUTATE, now=T0)
        self.assertTrue(ok.allowed)
        expired = service.evaluate(
            action, None, purpose=PURPOSE_MUTATE, now=T0 + timedelta(seconds=61)
        )
        self.assertTrue(expired.blocked)
        self.assertEqual(expired.reason_code, "github_readiness_expired")
        refreshed = await service.refresh(now=T0 + timedelta(seconds=120))
        self.assertEqual(refreshed.checked_at, T0 + timedelta(seconds=120))
        again = service.evaluate(
            action, None, purpose=PURPOSE_MUTATE, now=T0 + timedelta(seconds=120)
        )
        self.assertTrue(again.allowed)

    async def test_ae_af_per_repo(self):
        from side_effects.activation import PURPOSE_MUTATE
        from tests.side_effect_fixtures import github_action
        from workflow.engine import WorkflowEngine

        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello", 200)
        fake.seed_repository("octo", "other", 404)
        config = GitHubWriteAdapterConfig(
            enabled=True,
            allowed_repositories=("octo/hello", "octo/other"),
            dry_run=False,
            kill_switch=False,
            require_probe_success=True,
        )
        service = GitHubWriteActivationService(
            config=config, transport=fake, registered=True
        )
        await service.refresh()
        engine = WorkflowEngine()
        workflow_id = engine.create("task-se")
        action_a = github_action(workflow_id)
        action_b = github_action(
            workflow_id, resource="github://octo/other/issues/1/labels/bug"
        )
        decision_a = service.evaluate(action_a, None, purpose=PURPOSE_MUTATE)
        decision_b = service.evaluate(action_b, None, purpose=PURPOSE_MUTATE)
        self.assertTrue(decision_a.allowed)
        self.assertTrue(decision_b.blocked)
