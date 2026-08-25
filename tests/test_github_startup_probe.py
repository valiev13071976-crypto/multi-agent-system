import unittest

from side_effects.activation import WRITE_PERMISSION_CONFIRMED, WRITE_PERMISSION_UNCONFIRMED
from side_effects.github.models import GITHUB_TOOL_ID
from side_effects.github.transport import FakeGitHubTransport
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets
from tests.test_mode_auto import STRATEGY_TEXT
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app
from fastapi.testclient import TestClient


_ENABLED_PROBE = {
    "GITHUB_WRITE_ADAPTER_ENABLED": "true",
    "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
    "GITHUB_WRITE_DRY_RUN": "true",
    "GITHUB_WRITE_KILL_SWITCH": "true",
    "GITHUB_WRITE_PROBE_ON_STARTUP": "true",
    "GITHUB_WRITE_REQUIRE_PROBE_SUCCESS": "true",
}


class GitHubStartupProbeTests(unittest.IsolatedAsyncioTestCase):

    async def test_default_start_makes_no_github_calls(self):
        fake = FakeGitHubTransport()
        runtime = compose_side_effect_runtime(secrets=DictSecrets(), env={}, transport=fake)
        await runtime.start()
        self.assertFalse(runtime.startup_probe_ran)
        self.assertEqual(fake.get_repository_calls, 0)
        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(fake.remove_calls, 0)
        self.assertIsNone(runtime.registry.get(GITHUB_TOOL_ID))

    async def test_compose_does_not_call_github(self):
        fake = FakeGitHubTransport()
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env=_ENABLED_PROBE,
            transport=fake,
        )
        self.assertEqual(fake.get_repository_calls, 0)
        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(fake.remove_calls, 0)
        self.assertIsNotNone(runtime.registry.get(GITHUB_TOOL_ID))

    async def test_disabled_ignores_probe_on_startup(self):
        fake = FakeGitHubTransport()
        env = dict(_ENABLED_PROBE)
        env["GITHUB_WRITE_ADAPTER_ENABLED"] = "false"
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env=env,
            transport=fake,
        )
        await runtime.start()
        self.assertFalse(runtime.startup_probe_ran)
        self.assertEqual(fake.get_repository_calls, 0)
        self.assertEqual(fake.add_calls, 0)

    async def test_probe_on_startup_is_read_only(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello")
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env=_ENABLED_PROBE,
            transport=fake,
        )
        result = await runtime.start()
        self.assertTrue(runtime.startup_probe_ran)
        self.assertGreaterEqual(fake.get_repository_calls, 1)
        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(fake.remove_calls, 0)
        self.assertEqual(fake.get_calls, 0)
        self.assertIsNotNone(result)
        self.assertEqual(result.write_permission_status, WRITE_PERMISSION_UNCONFIRMED)
        self.assertNotEqual(result.write_permission_status, WRITE_PERMISSION_CONFIRMED)

    async def test_probe_failure_is_isolated(self):
        fake = FakeGitHubTransport()
        fake.seed_repository("octo", "hello", status=401)
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env=_ENABLED_PROBE,
            transport=fake,
        )
        result = await runtime.start()
        self.assertTrue(runtime.startup_probe_ran)
        self.assertEqual(fake.add_calls, 0)
        self.assertFalse(result.authenticated)
        self.assertEqual(runtime.config.kill_switch, True)
        self.assertEqual(runtime.config.dry_run, True)

    async def test_probe_exception_does_not_raise(self):
        fake = FakeGitHubTransport()

        async def boom(*args, **kwargs):
            raise RuntimeError("probe exploded")

        fake.get_repository = boom
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env=_ENABLED_PROBE,
            transport=fake,
        )
        result = await runtime.start()
        self.assertTrue(runtime.startup_probe_ran)
        self.assertIsNone(result)
        self.assertEqual(runtime.config.kill_switch, True)

    async def test_enabled_without_startup_probe_skips_network(self):
        fake = FakeGitHubTransport()
        env = dict(_ENABLED_PROBE)
        env["GITHUB_WRITE_PROBE_ON_STARTUP"] = "false"
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env=env,
            transport=fake,
        )
        await runtime.start()
        self.assertFalse(runtime.startup_probe_ran)
        self.assertEqual(fake.get_repository_calls, 0)
        self.assertEqual(fake.add_calls, 0)

    def test_analyze_still_seven_fields(self):
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
        self.assertEqual(len(CONTRACT_KEYS), 7)
        self.assertFalse(main_mod.side_effect_runtime.config.enabled)
        self.assertTrue(main_mod.side_effect_runtime.config.dry_run)
        self.assertTrue(main_mod.side_effect_runtime.config.kill_switch)
        self.assertFalse(main_mod.side_effect_runtime.config.probe_on_startup)
        self.assertFalse(main_mod.side_effect_runtime.startup_probe_ran)
