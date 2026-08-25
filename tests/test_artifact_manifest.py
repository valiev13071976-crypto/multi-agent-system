import unittest

from evals.manifest import (
    assert_version_bumped_if_content_changed,
    build_artifact_manifest,
    manifest_to_json,
)
from evals.models import ArtifactVersion, content_hash


class ArtifactManifestTests(unittest.TestCase):
    def test_deterministic_ordering_and_hash(self):
        m1 = build_artifact_manifest()
        m2 = build_artifact_manifest()
        self.assertEqual(m1["manifest_hash"], m2["manifest_hash"])
        self.assertEqual(manifest_to_json(m1), manifest_to_json(m2))
        types = [a["artifact_type"] for a in m1["artifacts"]]
        self.assertEqual(types, sorted(types))

    def test_includes_tool_prompt_policy_judge_validator(self):
        rows = build_artifact_manifest()["artifacts"]
        kinds = {(r["artifact_type"], r["artifact_id"]) for r in rows}
        self.assertIn(("tool_schema", "search"), kinds)
        self.assertIn(("prompt", "strategist"), kinds)
        self.assertIn(("policy", "autonomy_gate"), kinds)
        self.assertIn(("judge", "Judge"), kinds)
        self.assertIn(("validator", "structural"), kinds)
        self.assertIn(("router_policy", "model_router"), kinds)

    def test_no_secrets(self):
        blob = manifest_to_json(build_artifact_manifest()).lower()
        for needle in (
            "github_write_token",
            "panda_encryption_key",
            "sk-",
            "bearer ",
            "authorization",
        ):
            self.assertNotIn(needle, blob)

    def test_version_bump_required(self):
        a = ArtifactVersion("policy", "autonomy_gate", "1.0.0", "aaa")
        b = ArtifactVersion("policy", "autonomy_gate", "1.0.0", "bbb")
        with self.assertRaises(AssertionError):
            assert_version_bumped_if_content_changed(a, b)
        c = ArtifactVersion("policy", "autonomy_gate", "1.0.1", "bbb")
        assert_version_bumped_if_content_changed(a, c)

    def test_prompt_hash_newline_normalization(self):
        from evals.versions import prompt_content_hash

        self.assertEqual(
            prompt_content_hash("hello\r\nworld"),
            prompt_content_hash("hello\nworld"),
        )
        self.assertNotEqual(
            prompt_content_hash("hello world"),
            prompt_content_hash("hello  world"),
        )


if __name__ == "__main__":
    unittest.main()
