"""Stage-3 production validation harness tests."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production_validation.backup_drill import BackupDrillHarness
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.failure_harness import FailureInjectionHarness
from production_validation.isolation_harness import IsolationHarness
from production_validation.load_harness import LoadHarness
from production_validation.models import GateStatus
from production_validation.providers_live import LiveProviderValidator
from production_validation.recovery_harness import RecoveryHarness
from production_validation.release_gate import ReleaseGateEvaluator
from production_validation.security_probes import SecurityProbeHarness
from production_validation.smoke import SmokeRunner
from production_validation.soak_harness import SoakHarness


class Stage3EvidenceTests(unittest.TestCase):
    def test_evidence_immutable_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(root=tmp)
            from production_validation.models import ExecutionMode, ReleaseEvidence, VerificationClass

            ev = ReleaseEvidence.begin(gate="test", environment="local", mode=ExecutionMode.LOCAL_FIXTURE)
            ev.complete(status=GateStatus.PASS, classification=VerificationClass.CODE_VERIFIED.value)
            store.save(ev)
            path = Path(tmp) / f"{ev.evidence_id}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "PASS")


class Stage3HarnessTests(unittest.TestCase):
    def test_smoke_local(self):
        from tests.test_smoke import load_app

        config = ValidationConfig.from_env({"RELEASE_IDENTITY": "test"})
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="local")
            runner = SmokeRunner(config=config, store=EvidenceStore(root=tmp))
            out = runner.run_local(load_app().app)
            self.assertEqual(out["status"], "PASS")

    def test_security_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="local")
            out = SecurityProbeHarness(config=config, store=EvidenceStore(root=tmp)).run_local()
            self.assertEqual(out["status"], "PASS")

    def test_load_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="local", max_load_requests=20)
            out = LoadHarness(config=config, store=EvidenceStore(root=tmp)).run_local()
            self.assertEqual(out["status"], "PASS")
            self.assertGreater(out["metrics"]["requests"], 0)

    def test_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="local")
            out = IsolationHarness(config=config, store=EvidenceStore(root=tmp)).run()
            self.assertEqual(out["status"], "PASS")

    def test_soak_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="local", soak_duration_seconds=1.0)
            out = SoakHarness(config=config, store=EvidenceStore(root=tmp)).run_local()
            self.assertEqual(out["status"], "PASS")

    def test_failure_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="local")
            out = FailureInjectionHarness(config=config, store=EvidenceStore(root=tmp)).run()
            self.assertEqual(out["status"], "PASS")

    def test_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="local")
            out = RecoveryHarness(config=config, store=EvidenceStore(root=tmp)).run_worker_crash_simulation()
            self.assertEqual(out["status"], "PASS")

    def test_backup_drill(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="local")
            out = BackupDrillHarness(config=config, store=EvidenceStore(root=tmp)).run_isolated()
            self.assertEqual(out["status"], "PASS")

    def test_live_smoke_blocked_without_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="production")
            out = SmokeRunner(config=config, store=EvidenceStore(root=tmp)).run_live()
            self.assertEqual(out["status"], "BLOCKED")

    def test_provider_matrix_blocks_required_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="production")
            out = LiveProviderValidator(config=config, store=EvidenceStore(root=tmp), env={}).run_gate()
            self.assertEqual(out["status"], "BLOCKED")

    def test_release_gate_not_ready_without_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ValidationConfig(production_url="", release_identity="test", environment="production")
            local = {
                "3.3_smoke": "PASS",
                "3.5_security": "PASS",
                "3.6_load": "PASS",
                "3.7_isolation": "PASS",
                "3.8_soak": "PASS",
                "3.9_failure_injection": "PASS",
                "3.10_crash_recovery": "PASS",
                "3.11_backup_restore": "PASS",
            }
            gate = ReleaseGateEvaluator(config=config, store=EvidenceStore(root=tmp)).evaluate(local_results=local)
            self.assertEqual(gate.engineering, "PASS")
            self.assertEqual(gate.live_validation, "BLOCKED")
            self.assertEqual(gate.release_readiness, "NOT_READY")


class Stage3CliTests(unittest.TestCase):
    def test_cli_local_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["PANDA_RELEASE_EVIDENCE_ROOT"] = tmp
            from production_validation.cli import main

            with patch.dict(os.environ, {"PANDA_RELEASE_EVIDENCE_ROOT": tmp}, clear=False):
                rc = main(["local"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
