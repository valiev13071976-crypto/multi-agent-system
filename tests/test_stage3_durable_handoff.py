"""Durable Stage3 handoff artifact + EvidenceStore path tests (P1)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from controlled_launch.handoff import Stage3HandoffGate
from production_activation.access import PERM_ACTIVATION_AUTHORIZE, PERM_ACTIVATION_READ, PERM_ACTIVATION_WRITE
from production_activation.commands import PrepareActivationCommand
from production_activation.errors import ProductionActivationError
from production_activation.handoff import Stage5HandoffGate
from production_activation.service import ProductionActivationService
from production_activation.sqlite_store import SqliteProductionActivationStore
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore, resolve_release_evidence_root
from production_validation.operator_evidence import OperatorEvidenceRecorder
from production_validation.stage3_artifact import (
    Stage3HandoffArtifactError,
    load_stage3_handoff_artifact,
    require_stage3_artifact_ready,
)


def _admin():
    return SimpleNamespace(
        actor_ref=lambda: "ops",
        permissions=(PERM_ACTIVATION_READ, PERM_ACTIVATION_WRITE, PERM_ACTIVATION_AUTHORIZE, "operations:read"),
        roles=("PLATFORM_ADMIN",),
    )


def _valid_artifact(**overrides) -> dict:
    base = {
        "schema_version": "1",
        "stage": 3,
        "verdict": "PRODUCTION_VALIDATION_PASS",
        "engineering": "PASS",
        "live_validation": "PASS",
        "release_readiness": "READY",
        "blocked": [],
        "p0": [],
        "p1": [],
        "closed": True,
        "stage3_release_identity": "unavailable",
        "closure_timestamp": "unavailable",
        "provenance": {"classification": "IMMUTABLE_STAGE_CLOSURE"},
    }
    base.update(overrides)
    return base


def _write_artifact(tmp: str, data: dict) -> str:
    path = str(Path(tmp) / "STAGE3_HANDOFF.json")
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    return path


class Stage3ArtifactValidationTests(unittest.TestCase):
    def test_case1_valid_repo_artifact_closed_ready(self):
        data = load_stage3_handoff_artifact()
        require_stage3_artifact_ready(data)
        gate = Stage3HandoffGate(require_stage3_artifact=True)
        handoff = gate.evaluate()
        self.assertEqual(handoff.stage3_status, "CLOSED")
        self.assertEqual(handoff.release_readiness, "READY")
        self.assertEqual(handoff.p0_count, 0)
        self.assertEqual(handoff.p1_count, 0)

    def test_case2_missing_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Stage3HandoffArtifactError) as ctx:
                load_stage3_handoff_artifact(Path(tmp) / "missing.json")
            self.assertEqual(ctx.exception.code, "stage3_artifact_missing")

    def test_case3_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(Stage3HandoffArtifactError) as ctx:
                load_stage3_handoff_artifact(path)
            self.assertEqual(ctx.exception.code, "stage3_artifact_malformed")

    def test_case4_wrong_stage(self):
        with self.assertRaises(Stage3HandoffArtifactError) as ctx:
            require_stage3_artifact_ready(_valid_artifact(stage=4))
        self.assertEqual(ctx.exception.code, "wrong_stage")

    def test_case5_wrong_verdict(self):
        with self.assertRaises(Stage3HandoffArtifactError) as ctx:
            require_stage3_artifact_ready(_valid_artifact(verdict="FAIL"))
        self.assertEqual(ctx.exception.code, "verdict_not_pass")

    def test_case6_engineering(self):
        with self.assertRaises(Stage3HandoffArtifactError):
            require_stage3_artifact_ready(_valid_artifact(engineering="FAIL"))

    def test_case7_live_validation(self):
        with self.assertRaises(Stage3HandoffArtifactError):
            require_stage3_artifact_ready(_valid_artifact(live_validation="FAIL"))

    def test_case8_release_readiness(self):
        with self.assertRaises(Stage3HandoffArtifactError):
            require_stage3_artifact_ready(_valid_artifact(release_readiness="NOT_READY"))

    def test_case9_blocked(self):
        with self.assertRaises(Stage3HandoffArtifactError):
            require_stage3_artifact_ready(_valid_artifact(blocked=["x"]))

    def test_case10_p0(self):
        with self.assertRaises(Stage3HandoffArtifactError):
            require_stage3_artifact_ready(_valid_artifact(p0=["p0"]))

    def test_case11_p1(self):
        with self.assertRaises(Stage3HandoffArtifactError):
            require_stage3_artifact_ready(_valid_artifact(p1=["p1"]))

    def test_case12_closed_false(self):
        with self.assertRaises(Stage3HandoffArtifactError):
            require_stage3_artifact_ready(_valid_artifact(closed=False))


class Stage3EvidencePathTests(unittest.TestCase):
    def test_case13_panda_data_dir(self):
        root = resolve_release_evidence_root({"PANDA_DATA_DIR": "/data"})
        self.assertEqual(root.replace("\\", "/"), "/data/release_evidence")

    def test_case14_override_wins(self):
        root = resolve_release_evidence_root(
            {"PANDA_DATA_DIR": "/data", "PANDA_RELEASE_EVIDENCE_ROOT": "/custom/ev"}
        )
        self.assertEqual(root, "/custom/ev")

    def test_case15_writer_reader_same_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PANDA_DATA_DIR": tmp}
            store_a = EvidenceStore(env=env)
            store_b = EvidenceStore(env=env)
            self.assertEqual(store_a.root, store_b.root)
            self.assertTrue(str(store_a.root).endswith("release_evidence"))


class Stage3ArtifactHandoffIntegrationTests(unittest.TestCase):
    def test_case16_reconstructed_gate_without_cwd_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = EvidenceStore(root=str(Path(tmp) / "empty_ev"))
            config = ValidationConfig(production_url="", release_identity="new-patch-sha", environment="production")
            gate = Stage3HandoffGate(config=config, evidence_store=empty, require_stage3_artifact=True)
            handoff = gate.evaluate()
            self.assertEqual(handoff.stage3_status, "CLOSED")
            self.assertEqual(handoff.release_readiness, "READY")
            # Current deploy SHA must not be forced into Stage3 historical identity
            self.assertNotEqual(handoff.commit_sha, "new-patch-sha")

    def test_case17_wiped_cwd_evidence_artifact_still_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiped = Path(tmp) / "data" / "release_evidence"
            wiped.mkdir(parents=True)
            # empty directory — no ev-*.json
            empty = EvidenceStore(root=str(wiped))
            gate = Stage3HandoffGate(
                config=ValidationConfig(production_url="", release_identity="x", environment="production"),
                evidence_store=empty,
                require_stage3_artifact=True,
            )
            self.assertEqual(gate.evaluate().stage3_status, "CLOSED")
            gate.require_ready()

    def test_case18_stage5_validate_prerequisite_pass(self):
        gate = Stage5HandoffGate(require_stage4_artifact=True)
        # Default Stage3HandoffGate uses repo STAGE3_HANDOFF.json
        result = gate.go_live_gate_result(candidate_id="stage5-production")
        self.assertEqual(result, "GO_LIVE_GATE_PASS")
        handoff = gate.evaluate(candidate_id="stage5-production")
        self.assertEqual(handoff.stage3_status, "CLOSED")
        self.assertEqual(handoff.stage3_readiness, "READY")
        self.assertEqual(handoff.stage4_status, "CLOSED")
        self.assertEqual(handoff.promotion_decision, "GO_LIVE_ELIGIBLE")

    def test_case19_invalid_stage3_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = _write_artifact(tmp, _valid_artifact(verdict="BLOCKED"))
            empty = EvidenceStore(root=str(Path(tmp) / "ev"))
            config = ValidationConfig(production_url="", release_identity="x", environment="production")
            gate = Stage5HandoffGate(
                stage3_gate=Stage3HandoffGate(
                    config=config,
                    evidence_store=empty,
                    stage3_artifact_path=bad,
                    require_stage3_artifact=True,
                ),
                require_stage4_artifact=True,
            )
            self.assertEqual(gate.go_live_gate_result(candidate_id="stage5-production"), "GO_LIVE_BLOCKED")

    def test_case20_prepare_with_durable_handoffs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteProductionActivationStore(path=str(Path(tmp) / "pa.sqlite"))
            svc = ProductionActivationService(store=store)  # default handoffs use repo artifacts
            admin = _admin()
            out = svc.prepare(
                admin,
                PrepareActivationCommand(
                    candidate_id="stage5-production",
                    production_url="https://prod.example",
                    operator_ref="ops",
                    monitoring_destination="datadog",
                    alert_destination="pagerduty",
                ),
            )
            self.assertIn("candidate", out)
            self.assertIn("plan", out)
            self.assertFalse(svc.stage5_status(admin)["go_live_active"])
            store.close()

    def test_case21_no_activation(self):
        gate = Stage5HandoffGate()
        gate.require_ready(candidate_id="stage5-production")
        svc = ProductionActivationService(store=SqliteProductionActivationStore())
        self.assertFalse(svc.stage5_status(_admin())["go_live_active"])
        svc.store.close()

    def test_case22_artifact_load_does_not_create_live_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = EvidenceStore(root=str(Path(tmp) / "ev"))
            before = list(empty.root.glob("ev-*.json"))
            Stage3HandoffGate(
                config=ValidationConfig(production_url="", release_identity="x", environment="production"),
                evidence_store=empty,
                require_stage3_artifact=True,
            ).evaluate()
            after = list(empty.root.glob("ev-*.json"))
            self.assertEqual(before, after)

    def test_case23_stage5_sha_does_not_invalidate_stage3(self):
        gate = Stage3HandoffGate(
            config=ValidationConfig(production_url="", release_identity="070c634-new-patch", environment="production"),
            evidence_store=EvidenceStore(root=tempfile.mkdtemp()),
            require_stage3_artifact=True,
        )
        # Must not raise STALE merely because current config SHA differs
        gate.require_ready(commit_sha="070c634-new-patch")


class Stage3ArtifactFailClosedGateTests(unittest.TestCase):
    def test_missing_required_artifact_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = Stage3HandoffGate(
                config=ValidationConfig(production_url="", release_identity="x", environment="production"),
                evidence_store=EvidenceStore(root=str(Path(tmp) / "ev")),
                stage3_artifact_path=str(Path(tmp) / "missing.json"),
                require_stage3_artifact=True,
            )
            handoff = gate.evaluate()
            self.assertEqual(handoff.stage3_status, "OPEN")
            self.assertEqual(handoff.release_readiness, "NOT_READY")


if __name__ == "__main__":
    unittest.main()
