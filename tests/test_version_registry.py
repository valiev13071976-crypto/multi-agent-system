import unittest

from evals.models import ArtifactVersion, content_hash
from evals.registry import (
    VersionConflictError,
    VersionNotFoundError,
    VersionRegistry,
)


class VersionRegistryTests(unittest.TestCase):
    def test_register_get_list_current(self):
        reg = VersionRegistry()
        a = ArtifactVersion("policy", "autonomy_gate", "1.0.0", content_hash({"v": 1}))
        reg.register(a)
        self.assertEqual(reg.get("policy", "autonomy_gate", "1.0.0"), a)
        self.assertEqual(reg.current_version("policy", "autonomy_gate"), "1.0.0")
        self.assertEqual(len(reg.list_versions("policy", "autonomy_gate")), 1)
        self.assertEqual(reg.resolve("policy", "autonomy_gate").version, "1.0.0")

    def test_deterministic_hash(self):
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))

    def test_duplicate_same_hash_idempotent(self):
        reg = VersionRegistry()
        h = content_hash({"x": 1})
        a = ArtifactVersion("prompt", "strategist", "1.0.0", h)
        b = ArtifactVersion("prompt", "strategist", "1.0.0", h)
        self.assertIs(reg.register(a), reg.register(b))

    def test_same_version_different_hash_denied(self):
        reg = VersionRegistry()
        reg.register(ArtifactVersion("prompt", "strategist", "1.0.0", "aaa"))
        with self.assertRaises(VersionConflictError):
            reg.register(ArtifactVersion("prompt", "strategist", "1.0.0", "bbb"))

    def test_unknown_version(self):
        reg = VersionRegistry()
        with self.assertRaises(VersionNotFoundError):
            reg.get("policy", "x", "9.9.9")
        with self.assertRaises(VersionNotFoundError):
            reg.current_version("policy", "x")

    def test_compare(self):
        left = ArtifactVersion("judge", "Judge", "1.0.0", "aaa")
        right = ArtifactVersion("judge", "Judge", "1.0.1", "bbb")
        cmp = VersionRegistry().compare(left, right)
        self.assertFalse(cmp["same_identity"])
        self.assertTrue(cmp["hash_changed"])


if __name__ == "__main__":
    unittest.main()
