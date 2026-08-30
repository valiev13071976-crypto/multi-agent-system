"""Stage-3 operator live evidence ingestion tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

from production_validation.cli import main as cli_main
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass
from production_validation.operator_evidence import OperatorEvidenceError, OperatorEvidenceRecorder
from production_validation.providers_live import LiveProviderValidator
from production_validation.release_gate import MANDATORY_LIVE_GATES, ReleaseGateEvaluator


def _config(*, release_identity: str = "rel-stage3", production_url: str = "https://prod.example") -> ValidationConfig:
    return ValidationConfig(
        production_url=production_url,
        release_identity=release_identity,
        environment="production",
    )


def _seed(store: EvidenceStore, *, gate: str, status: GateStatus, classification: str, release_identity: str = "rel-stage3") -> ReleaseEvidence:
    ev = ReleaseEvidence.begin(
        gate=gate,
        environment="production",
        mode=ExecutionMode.LIVE_SAFE,
        classification=classification,
        release_identity=release_identity,
    )
    ev.complete(status=status, classification=classification)
    store.save(ev)
    return ev


class OperatorEvidenceRecorderTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = EvidenceStore(root=self._tmpdir.name)
        self.config = _config()
        self.recorder = OperatorEvidenceRecorder(config=self.config, store=self.store)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_case1_pass_with_confirm(self):
        out = self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="manual-stage3-operator",
            note="Railway /api/analyze returned openai: PANDA_LIVE_OK",
            confirm_live_verified=True,
        )
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(out["classification"], VerificationClass.LIVE_VERIFIED.value)
        self.assertEqual(out["gate"], "3.2_ai_live")
        self.assertTrue(out["evidence_id"])

    def test_case2_pass_without_confirm_rejected(self):
        with self.assertRaises(OperatorEvidenceError) as ctx:
            self.recorder.record(
                gate="3.2_ai_live",
                status="PASS",
                operator="manual-stage3-operator",
                note="note",
                confirm_live_verified=False,
            )
        self.assertEqual(ctx.exception.code, "confirm_live_verified_required")

    def test_case3_unknown_gate_rejected(self):
        with self.assertRaises(OperatorEvidenceError) as ctx:
            self.recorder.record(
                gate="9.9_unknown",
                status="PASS",
                operator="ops",
                note="note",
                confirm_live_verified=True,
            )
        self.assertEqual(ctx.exception.code, "gate_not_allowed")

    def test_case4_release_gate_manual_pass_rejected(self):
        with self.assertRaises(OperatorEvidenceError) as ctx:
            self.recorder.record(
                gate="3.17_release_gate",
                status="PASS",
                operator="ops",
                note="note",
                confirm_live_verified=True,
            )
        self.assertEqual(ctx.exception.code, "gate_forbidden")

    def test_case5_local_engineering_gate_rejected(self):
        with self.assertRaises(OperatorEvidenceError) as ctx:
            self.recorder.record(
                gate="3.6_load",
                status="PASS",
                operator="ops",
                note="note",
                confirm_live_verified=True,
            )
        self.assertEqual(ctx.exception.code, "gate_forbidden")

    def test_case6_empty_operator_rejected(self):
        with self.assertRaises(OperatorEvidenceError) as ctx:
            self.recorder.record(
                gate="3.2_ai_live",
                status="PASS",
                operator="  ",
                note="note",
                confirm_live_verified=True,
            )
        self.assertEqual(ctx.exception.code, "operator_required")

    def test_case7_empty_note_rejected(self):
        with self.assertRaises(OperatorEvidenceError) as ctx:
            self.recorder.record(
                gate="3.2_ai_live",
                status="PASS",
                operator="ops",
                note="",
                confirm_live_verified=True,
            )
        self.assertEqual(ctx.exception.code, "note_required")

    def test_case8_evidence_persisted(self):
        out = self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="ok",
            confirm_live_verified=True,
        )
        path = Path(self._tmpdir.name) / f"{out['evidence_id']}.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "PASS")
        self.assertEqual(data["classification"], "LIVE_VERIFIED")

    def test_case9_historical_blocked_not_deleted(self):
        blocked = _seed(
            self.store,
            gate="3.2_ai_live",
            status=GateStatus.BLOCKED,
            classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
        )
        blocked_path = Path(self._tmpdir.name) / f"{blocked.evidence_id}.json"
        self.assertTrue(blocked_path.exists())
        self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="now verified",
            confirm_live_verified=True,
        )
        self.assertTrue(blocked_path.exists())
        old = json.loads(blocked_path.read_text(encoding="utf-8"))
        self.assertEqual(old["status"], "BLOCKED")
        self.assertTrue(old.get("superseded_by"))

    def test_case10_later_pass_is_latest(self):
        _seed(self.store, gate="3.2_ai_live", status=GateStatus.BLOCKED, classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value)
        out = self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="verified",
            confirm_live_verified=True,
        )
        latest = self.store.latest_for_gate("3.2_ai_live")
        self.assertEqual(latest["evidence_id"], out["evidence_id"])
        self.assertEqual(latest["status"], "PASS")

    def test_case11_later_blocked_becomes_current(self):
        self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="verified",
            confirm_live_verified=True,
        )
        out = self.recorder.record(
            gate="3.2_ai_live",
            status="BLOCKED",
            operator="ops",
            note="regression observed",
            confirm_live_verified=False,
        )
        latest = self.store.latest_for_gate("3.2_ai_live")
        self.assertEqual(latest["evidence_id"], out["evidence_id"])
        self.assertEqual(latest["status"], "BLOCKED")

    def test_case12_one_gate_pass_does_not_close_others(self):
        self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="ok",
            confirm_live_verified=True,
        )
        gate = ReleaseGateEvaluator(config=self.config, store=self.store).evaluate()
        self.assertIn("3.1_railway_deployment", gate.blocked)
        self.assertNotEqual(gate.release_readiness, "READY")

    def test_case13_release_gate_blocked_while_missing(self):
        self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="ok",
            confirm_live_verified=True,
        )
        gate = ReleaseGateEvaluator(config=self.config, store=self.store).evaluate()
        self.assertEqual(gate.verdict, "PRODUCTION_VALIDATION_BLOCKED")
        self.assertTrue(gate.blocked)

    def test_case14_all_mandatory_pass_makes_ready(self):
        for gate_name in MANDATORY_LIVE_GATES:
            if gate_name in {
                "3.1_railway_deployment",
                "3.1_restart_persistence",
                "3.2_ai_live",
                "3.5_security_live",
                "3.11_backup_restore_live",
                "3.12_alerts_live",
                "3.15_rollback_live",
            }:
                self.recorder.record(
                    gate=gate_name,
                    status="PASS",
                    operator="ops",
                    note=f"verified {gate_name}",
                    confirm_live_verified=True,
                )
            else:
                _seed(
                    self.store,
                    gate=gate_name,
                    status=GateStatus.PASS,
                    classification=VerificationClass.LIVE_VERIFIED.value,
                )
        local = {
            "3.1_env_config": "PASS",
            "3.3_smoke": "PASS",
            "3.5_security": "PASS",
            "3.6_load": "PASS",
            "3.7_isolation": "PASS",
            "3.8_soak": "PASS",
            "3.9_failure_injection": "PASS",
            "3.10_crash_recovery": "PASS",
            "3.11_backup_restore": "PASS",
        }
        gate = ReleaseGateEvaluator(config=self.config, store=self.store).evaluate(local_results=local)
        self.assertEqual(gate.release_readiness, "READY")
        self.assertEqual(gate.verdict, "PRODUCTION_VALIDATION_PASS")
        self.assertEqual(gate.blocked, [])

    def test_case15_release_identity_bound(self):
        out = self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="ok",
            confirm_live_verified=True,
        )
        self.assertEqual(out["release_identity"], "rel-stage3")
        latest = self.store.latest_for_gate("3.2_ai_live")
        self.assertEqual(latest["release_identity"], "rel-stage3")

    def test_case16_release_identity_mismatch_rejected(self):
        with self.assertRaises(OperatorEvidenceError) as ctx:
            self.recorder.record(
                gate="3.2_ai_live",
                status="PASS",
                operator="ops",
                note="ok",
                confirm_live_verified=True,
                release_identity="other-release",
            )
        self.assertEqual(ctx.exception.code, "release_identity_mismatch")

    def test_case17_18_cli_structured_safe_output(self):
        import os

        env = {
            **os.environ,
            "PANDA_RELEASE_EVIDENCE_ROOT": self._tmpdir.name,
            "RELEASE_IDENTITY": "rel-stage3",
            "PUBLIC_URL": "https://prod.example",
            "PANDA_ENV": "production",
        }
        buf = io.StringIO()
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            with redirect_stdout(buf):
                code = cli_main(
                    [
                        "record-live",
                        "--gate",
                        "3.2_ai_live",
                        "--status",
                        "PASS",
                        "--operator",
                        "manual-stage3-operator",
                        "--note",
                        "Bounded OpenAI request returned PANDA_LIVE_OK",
                        "--confirm-live-verified",
                    ]
                )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        for key in ("status", "gate", "classification", "evidence_id", "environment", "release_identity"):
            self.assertIn(key, payload)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["classification"], "LIVE_VERIFIED")
        dumped = json.dumps(payload)
        self.assertNotIn("sk-", dumped)
        self.assertNotIn("Authorization", dumped)

    def test_case19_ai_live_satisfies_openai_provider_matrix(self):
        self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="openai live verified",
            confirm_live_verified=True,
        )
        validator = LiveProviderValidator(
            config=self.config,
            store=self.store,
            env={"OPENAI_API_KEY": "sk-live-not-placeholder-value-123456"},
        )
        matrix = validator.build_matrix()
        openai = next(r for r in matrix if r["provider"] == "openai")
        self.assertEqual(openai["verification"], VerificationClass.LIVE_VERIFIED.value)
        self.assertEqual(openai["status"], "PASS")
        result = validator.run_gate()
        self.assertEqual(result["status"], "PASS")

    def test_case20_optional_providers_non_blocking(self):
        self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="openai live verified",
            confirm_live_verified=True,
        )
        validator = LiveProviderValidator(
            config=self.config,
            store=self.store,
            env={"OPENAI_API_KEY": "sk-live-not-placeholder-value-123456"},
        )
        matrix = validator.build_matrix()
        optional = [r for r in matrix if r["requirement"] != "REQUIRED_FOR_STAGE4"]
        self.assertTrue(optional)
        for row in optional:
            self.assertNotEqual(row["status"], "FAIL")
        result = validator.run_gate()
        self.assertEqual(result["status"], "PASS")

    def test_verify_required_ai_live_preserves_operator_pass(self):
        self.recorder.record(
            gate="3.2_ai_live",
            status="PASS",
            operator="ops",
            note="verified",
            confirm_live_verified=True,
        )
        validator = LiveProviderValidator(config=self.config, store=self.store, env={"OPENAI_API_KEY": "sk-live-not-placeholder-value-123456"})
        out = validator.verify_required_ai_live()
        self.assertEqual(out["status"], "PASS")
        latest = self.store.latest_for_gate("3.2_ai_live")
        self.assertEqual(latest["status"], "PASS")

    def test_secret_like_note_rejected(self):
        with self.assertRaises(OperatorEvidenceError) as ctx:
            self.recorder.record(
                gate="3.2_ai_live",
                status="PASS",
                operator="ops",
                note="key=sk-abcdefghijklmnopqrstuvwxyz",
                confirm_live_verified=True,
            )
        self.assertEqual(ctx.exception.code, "secret_like_input_rejected")

    def test_cli_rejects_pass_without_confirm(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(
                [
                    "record-live",
                    "--gate",
                    "3.2_ai_live",
                    "--status",
                    "PASS",
                    "--operator",
                    "ops",
                    "--note",
                    "missing confirm",
                ]
            )
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"], "confirm_live_verified_required")


class LatestEvidenceSelectionTests(unittest.TestCase):
    def test_release_gate_uses_latest_not_file_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(root=tmp)
            config = _config()
            # Seed older PASS then newer BLOCKED for one gate; leave others missing.
            old = _seed(store, gate="3.2_ai_live", status=GateStatus.PASS, classification=VerificationClass.LIVE_VERIFIED.value)
            new = _seed(store, gate="3.2_ai_live", status=GateStatus.BLOCKED, classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value)
            # Force completed_at ordering: rewrite timestamps if needed
            old_path = Path(tmp) / f"{old.evidence_id}.json"
            new_path = Path(tmp) / f"{new.evidence_id}.json"
            old_data = json.loads(old_path.read_text(encoding="utf-8"))
            new_data = json.loads(new_path.read_text(encoding="utf-8"))
            old_data["completed_at"] = "2020-01-01T00:00:00+00:00"
            new_data["completed_at"] = "2026-01-01T00:00:00+00:00"
            old_path.write_text(json.dumps(old_data, indent=2, sort_keys=True), encoding="utf-8")
            new_path.write_text(json.dumps(new_data, indent=2, sort_keys=True), encoding="utf-8")
            gate = ReleaseGateEvaluator(config=config, store=store).evaluate()
            self.assertEqual(gate.gates.get("3.2_ai_live"), "BLOCKED")
            self.assertIn("3.2_ai_live", gate.blocked)


if __name__ == "__main__":
    unittest.main()
