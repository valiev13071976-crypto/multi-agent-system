import unittest

from side_effects.factory import build_production_side_effect_registry
from side_effects.github.config import GitHubWriteAdapterConfig, parse_allowed_repositories
from side_effects.github.errors import GitHubWriteConfigError
from side_effects.github.models import GITHUB_TOOL_ID


class DictSecrets:
    def __init__(self, mapping=None):
        self.mapping = dict(mapping or {})

    def get(self, name):
        return self.mapping.get(name)


class GitHubWriteConfigTests(unittest.TestCase):

    def test_i_default_disabled(self):
        config = GitHubWriteAdapterConfig.from_env({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.allowed_repositories, ())

    def test_j_disabled_missing_token_not_registered(self):
        registry = build_production_side_effect_registry(
            secrets=DictSecrets(),
            env={"GITHUB_WRITE_ADAPTER_ENABLED": "false"},
        )
        self.assertIsNone(registry.get(GITHUB_TOOL_ID))

    def test_k_enabled_missing_token_fail_closed(self):
        with self.assertRaises(GitHubWriteConfigError) as caught:
            build_production_side_effect_registry(
                secrets=DictSecrets(),
                env={
                    "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                    "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
                },
            )
        self.assertEqual(caught.exception.error_code, "github_token_missing")

    def test_l_enabled_empty_allowlist_fail_closed(self):
        with self.assertRaises(GitHubWriteConfigError) as caught:
            build_production_side_effect_registry(
                secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
                env={"GITHUB_WRITE_ADAPTER_ENABLED": "true"},
            )
        self.assertEqual(caught.exception.error_code, "github_allowlist_empty")

    def test_n_exact_allowed_repo_eligible(self):
        config = GitHubWriteAdapterConfig(
            enabled=True, allowed_repositories=("Octo/Hello",)
        )
        self.assertTrue(config.allows("octo", "hello"))
        self.assertEqual(config.allowed_repositories, ("octo/hello",))

    def test_o_wildcard_allowlist_rejected(self):
        with self.assertRaises(GitHubWriteConfigError):
            parse_allowed_repositories("octo/*")
        with self.assertRaises(GitHubWriteConfigError):
            GitHubWriteAdapterConfig(allowed_repositories=("*",))

    def test_p_malformed_repo_rejected(self):
        with self.assertRaises(GitHubWriteConfigError):
            parse_allowed_repositories("https://github.com/octo/hello")
        with self.assertRaises(GitHubWriteConfigError):
            parse_allowed_repositories("not-a-repo")
        with self.assertRaises(GitHubWriteConfigError):
            parse_allowed_repositories("octo/hello/extra")

    def test_timeout_must_be_positive(self):
        with self.assertRaises(GitHubWriteConfigError):
            GitHubWriteAdapterConfig(timeout_seconds=0)

    def test_duplicates_removed(self):
        repos = parse_allowed_repositories("octo/hello,OCTO/HELLO,other/repo")
        self.assertEqual(repos, ("octo/hello", "other/repo"))

    def test_bd_factory_registers_only_when_complete(self):
        registry = build_production_side_effect_registry(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env={
                "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
            },
        )
        adapter = registry.get(GITHUB_TOOL_ID)
        self.assertIsNotNone(adapter)
        from side_effects.github.transport import GitHubHttpTransport

        self.assertIsInstance(adapter._transport, GitHubHttpTransport)
