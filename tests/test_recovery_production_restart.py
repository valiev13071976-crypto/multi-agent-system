"""P12.1 production restart durability without RECOVERY_DB_PATH."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recovery.models import (
    ACTION_RECONCILE_READ_ONLY,
    CASE_UNCERTAIN_SIDE_EFFECT,
    DECISION_DEFER,
    SEVERITY_NORMAL,
)
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets


class RecoveryProductionRestartTests(unittest.TestCase):
    def test_restart_restores_case_decision_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "prod-rec.sqlite3")
            env = {
                "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                "SIDE_EFFECT_DB_PATH": path,
                "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                "RECOVERY_ORCHESTRATION_ENABLED": "true",
                # RECOVERY_DB_PATH intentionally unset
            }
            runtime_a = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                orch_a = runtime_a.recovery_orchestrator
                self.assertEqual(orch_a.store.connection_mode, "shared")
                case = orch_a.create_case(
                    execution_id="exec-restart",
                    case_type=CASE_UNCERTAIN_SIDE_EFFECT,
                    enqueue=False,
                )
                orch_a.record_decision(
                    case.recovery_id,
                    DECISION_DEFER,
                    actor_id="op-1",
                    reason_code="later",
                )
                job = orch_a.queue.enqueue(
                    recovery_id=case.recovery_id,
                    action_type=ACTION_RECONCILE_READ_ONLY,
                    priority=SEVERITY_NORMAL,
                )
                recovery_id = case.recovery_id
                job_id = job.job_id
            finally:
                runtime_a.close()

            runtime_b = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                orch_b = runtime_b.recovery_orchestrator
                self.assertEqual(orch_b.store.connection_mode, "shared")
                loaded = orch_b.get_case(recovery_id)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.execution_id, "exec-restart")
                self.assertEqual(loaded.operator_decision, DECISION_DEFER)
                decisions = orch_b.store.list_decisions(recovery_id)
                self.assertEqual(len(decisions), 1)
                jobs = {j.job_id: j for j in orch_b.queue.list_jobs()}
                self.assertIn(job_id, jobs)
                self.assertEqual(jobs[job_id].recovery_id, recovery_id)
            finally:
                runtime_b.close()


if __name__ == "__main__":
    unittest.main()
