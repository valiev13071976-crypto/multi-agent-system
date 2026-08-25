import unittest

from side_effects.github.config import GitHubWriteAdapterConfig, parse_allowed_repositories
from side_effects.github.errors import GitHubWriteConfigError
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets


class GitHubActivationConfigTests(unittest.TestCase):

    def test_a_default_safe(self):
        config = GitHubWriteAdapterConfig.from_env({})
        self.assertFalse(config.enabled)
        self.assertTrue(config.dry_run)
        self.assertTrue(config.kill_switch)
        self.assertFalse(config.probe_on_startup)
        self.assertTrue(config.require_probe_success)

    def test_b_enabled_false_not_registered(self):
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets(),
            env={"GITHUB_WRITE_ADAPTER_ENABLED": "false"},
        )
        self.assertIsNone(runtime.registry.get("github.issue_labels"))

    def test_c_enabled_missing_token_isolated(self):
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets(),
            env={
                "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
                "GITHUB_WRITE_DRY_RUN": "true",
                "GITHUB_WRITE_KILL_SWITCH": "true",
            },
        )
        self.assertEqual(runtime.composition_error, "github_write_secret_missing")
        self.assertIsNone(runtime.registry.get("github.issue_labels"))

    def test_d_enabled_empty_allowlist_isolated(self):
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env={"GITHUB_WRITE_ADAPTER_ENABLED": "true"},
        )
        self.assertEqual(runtime.composition_error, "github_allowlist_empty")

    def test_e_wildcard_rejected(self):
        with self.assertRaises(GitHubWriteConfigError):
            parse_allowed_repositories("octo/*")

    def test_f_timeout_invalid(self):
        with self.assertRaises(GitHubWriteConfigError):
            GitHubWriteAdapterConfig(timeout_seconds=-1)

    def test_config_repr_has_no_token(self):
        text = repr(GitHubWriteAdapterConfig(enabled=True, allowed_repositories=("octo/hello",)))
        self.assertNotIn("ghs_", text)
        self.assertNotIn("token", text.lower())
        self.assertNotIn("GITHUB_WRITE_TOKEN", text)
