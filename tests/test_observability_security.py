import unittest

from observability.events import make_event
from observability.runtime import ObservabilityRuntime
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.security import sanitize_observability_metadata


class ObservabilitySecurityTests(unittest.TestCase):
    def test_secrets_stripped(self):
        cleaned, truncated = sanitize_observability_metadata(
            {
                "Authorization": "Bearer supersecret",
                "api_key": "k",
                "prompt": "do not store",
                "ok": "safe",
                "GITHUB_WRITE_TOKEN": "ghp_x",
                "PANDA_ENCRYPTION_KEY": "enc",
            }
        )
        blob = str(cleaned)
        for needle in (
            "Bearer supersecret",
            "ghp_x",
            "enc",
            "do not store",
            "Authorization",
        ):
            self.assertNotIn(needle, blob)
        self.assertEqual(cleaned.get("ok"), "safe")

    def test_event_buffer_has_no_secrets(self):
        runtime = ObservabilityRuntime(
            sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
        )
        runtime.emit(
            "tool.requested",
            context=runtime.create_context(),
            metadata={
                "Authorization": "Bearer abc",
                "arguments": {"token": "secret"},
                "note": "ok",
            },
            update_metrics=False,
        )
        blob = str(runtime.list_events()[0].metadata_safe)
        self.assertNotIn("Bearer abc", blob)
        self.assertNotIn("secret", blob)


if __name__ == "__main__":
    unittest.main()
