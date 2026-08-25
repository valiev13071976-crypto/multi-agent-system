"""Security: no secrets in recovery persistence/events."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime
from recovery.models import CASE_UNCERTAIN_SIDE_EFFECT, DECISION_BLOCK
from recovery.orchestrator import RecoveryOrchestrator
from recovery.store import SqliteRecoveryCaseStore


FORBIDDEN = (
    "GITHUB_WRITE_TOKEN=abc",
    "sk-openai-secret",
    "permit-raw-token-xyz",
    "capability-raw-xyz",
    "raw prompt secret text",
    '{"body":"secret-response"}',
)


class RecoverySecurityTests(unittest.TestCase):
    def test_no_secrets_in_db_or_events(self):
        sink = InMemoryObservabilitySink()
        obs = ObservabilityRuntime(sink=sink, metrics=MetricsCollector())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rec.sqlite3"
            store = SqliteRecoveryCaseStore(path)
            orch = RecoveryOrchestrator(
                store=store,
                observability=obs,
                enqueue_reconcile_on_create=False,
            )
            case = orch.create_case(
                execution_id="e1",
                case_type=CASE_UNCERTAIN_SIDE_EFFECT,
                metadata_safe={
                    "note": "safe",
                    # attempted injection should be sanitized away by sanitize_metadata
                    "authorization": "Bearer leak",
                    "token": "GITHUB_WRITE_TOKEN=abc",
                },
                enqueue=False,
            )
            orch.record_decision(
                case.recovery_id,
                DECISION_BLOCK,
                actor_id="op",
                reason_code="secure",
                note_safe="ok",
            )
            blob = path.read_bytes()
            text = blob.decode("utf-8", errors="ignore")
            events = json.dumps(
                [
                    {
                        "type": e.event_type,
                        "meta": dict(e.metadata_safe),
                    }
                    for e in sink.list_events()
                ]
            )
            combined = text + events + json.dumps(dict(case.metadata_safe))
            for needle in FORBIDDEN:
                self.assertNotIn(needle, combined)
            store.close()


if __name__ == "__main__":
    unittest.main()
