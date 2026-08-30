"""Extended Stage-5 GO LIVE control tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from controlled_launch.handoff import Stage3HandoffGate
from production_activation.access import PERM_ACTIVATION_AUTHORIZE, PERM_ACTIVATION_READ, PERM_ACTIVATION_WRITE
from production_activation.errors import ProductionActivationError, STAGE5_BLOCKED_BY_STAGE4
from production_activation.handoff import Stage5HandoffGate
from production_activation.models import ProductionActivationEvidence, VerificationClass
from production_activation.policy import create_policy, mark_activated, mark_deactivated
from production_activation.service import ProductionActivationService
from production_activation.sqlite_store import SqliteProductionActivationStore
from production_activation.stage4_artifact import load_stage4_handoff_artifact, require_stage4_artifact_ready
from production_activation.stage5_gate import MANDATORY_STAGE5_GATES, Stage5ReleaseGate
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass as PVClass
from production_validation.release_gate import MANDATORY_LIVE_GATES


def _admin():
    return SimpleNamespace(
        actor_ref=lambda: "ops",
        permissions=(PERM_ACTIVATION_READ, PERM_ACTIVATION_WRITE, PERM_ACTIVATION_AUTHORIZE, "operations:read"),
        roles=("PLATFORM_ADMIN",),
    )


def _seed_stage3(tmp: str, release_identity: str = "rel-s5"):
    store = EvidenceStore(root=tmp)
    config = ValidationConfig(production_url="https://prod.example", release_identity=release_identity, environment="production")
    for gate_name in MANDATORY_LIVE_GATES:
        g = ReleaseEvidence.begin(gate=gate_name, environment="production", mode=ExecutionMode.LIVE_SAFE, release_identity=release_identity)
        g.complete(status=GateStatus.PASS, classification=PVClass.LIVE_VERIFIED.value)
        store.save(g)
    return store, config


class Stage4ArtifactTests(unittest.TestCase):
    def test_malformed_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(ProductionActivationError):
                load_stage4_handoff_artifact(path)

    def test_blocked_verdict_rejected(self):
        with self.assertRaises(ProductionActivationError) as ctx:
            require_stage4_artifact_ready(
                {
                    "verdict": "CONTROLLED_LAUNCH_BLOCKED",
                    "engineering": "PASS",
                    "controlled_launch": "PASS",
                    "go_live_eligibility": "ELIGIBLE",
                    "go_live_active": False,
                    "blocked": ["x"],
                    "p0": [],
                    "p1": [],
                }
            )
        self.assertIn(ctx.exception.code, {"BLOCKED_BY_PREVIOUS_STAGE", STAGE5_BLOCKED_BY_STAGE4})

    def test_repo_artifact_ready(self):
        data = load_stage4_handoff_artifact()
        require_stage4_artifact_ready(data)
        self.assertEqual(data["go_live_eligibility"], "ELIGIBLE")


class Stage5PolicyTests(unittest.TestCase):
    def test_default_inactive(self):
        p = create_policy(release_identity="r1", created_by="ops")
        self.assertFalse(p.go_live_active)
        self.assertFalse(p.enabled)

    def test_activation_marks_active(self):
        p = create_policy(release_identity="r1", created_by="ops")
        active = mark_activated(p, activated_by="ops")
        self.assertTrue(active.go_live_active)
        deactivated = mark_deactivated(active, reason="pause")
        self.assertFalse(deactivated.go_live_active)


class Stage5GateTests(unittest.TestCase):
    def test_missing_mandatory_blocks(self):
        result = Stage5ReleaseGate().evaluate(evidence=[], stage4_handoff_pass=True)
        self.assertEqual(result.verdict, "GO_LIVE_BLOCKED")
        self.assertFalse(result.go_live_active)

    def test_all_pass_ready_not_active(self):
        evidence = [
            ProductionActivationEvidence.create(
                candidate_id="c1",
                deployment_id="",
                environment="production",
                plan_id="",
                attempt_id="",
                activation_state="",
                acceptance_result="PASS",
                classification=VerificationClass.CODE_VERIFIED.value,
                safe_metrics={"gate": g, "status": "PASS"},
            )
            for g in MANDATORY_STAGE5_GATES
        ]
        evidence.append(
            ProductionActivationEvidence.create(
                candidate_id="c1",
                deployment_id="",
                environment="production",
                plan_id="",
                attempt_id="",
                activation_state="",
                acceptance_result="FAIL",
                classification=VerificationClass.CODE_VERIFIED.value,
                safe_metrics={"gate": "5.16_post_activation_live", "status": "FAIL"},
            )
        )
        result = Stage5ReleaseGate().evaluate(evidence=evidence, stage4_handoff_pass=True)
        self.assertEqual(result.verdict, "GO_LIVE_READY")
        self.assertTrue(result.go_live_eligible)
        self.assertFalse(result.go_live_active)
        self.assertTrue(result.operator_action_required)


class Stage5NoImplicitActivationTests(unittest.TestCase):
    def test_import_and_status_do_not_activate(self):
        from production_activation import ProductionActivationService as Svc

        store = SqliteProductionActivationStore()
        svc = Svc(store=store, handoff_gate=Stage5HandoffGate(require_stage4_artifact=False))
        admin = _admin()
        status = svc.stage5_status(admin)
        self.assertFalse(status["go_live_active"])
        gate = svc.evaluate_stage5_gate(admin, candidate_id="")
        self.assertFalse(gate["go_live_active"])
        self.assertNotEqual(gate["verdict"], "GO_LIVE_PASS")
        store.close()


class Stage5ServiceControlsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        ev_store, config = _seed_stage3(str(Path(self._tmpdir.name) / "ev"))
        self.store = SqliteProductionActivationStore(path=str(Path(self._tmpdir.name) / "pa.sqlite"))
        self.svc = ProductionActivationService(
            store=self.store,
            handoff_gate=Stage5HandoffGate(
                stage3_gate=Stage3HandoffGate(config=config, evidence_store=ev_store),
                require_stage4_artifact=True,
            ),
        )
        self.admin = _admin()
        self.release = config.release_identity

    def tearDown(self):
        self.store.close()
        self._tmpdir.cleanup()

    def test_policy_seed_evaluate_deactivate_persist(self):
        policy = self.svc.create_go_live_policy(self.admin, release_identity=self.release)
        self.assertFalse(policy["go_live_active"])
        self.svc.seed_stage5_evidence(self.admin, candidate_id="lc-s5", release_identity=self.release)
        gate = self.svc.evaluate_stage5_gate(self.admin, candidate_id="lc-s5")
        self.assertEqual(gate["verdict"], "GO_LIVE_READY")
        self.assertTrue(gate["go_live_eligible"])
        self.assertFalse(gate["go_live_active"])
        self.assertTrue(gate["operator_action_required"])
        self.assertEqual(gate["blocked"], [])

        # Deactivate when not active remains safe/idempotent via rollback path
        out = self.svc.deactivate(self.admin, candidate_id="lc-s5", operator_ref="ops", reason="preemptive")
        self.assertEqual(out["state"], "ROLLED_BACK")

        svc2 = ProductionActivationService(
            store=SqliteProductionActivationStore(path=self.store.path),
            handoff_gate=self.svc.handoff_gate,
        )
        loaded = svc2.store.latest_go_live_policy()
        self.assertIsNotNone(loaded)
        self.assertFalse(loaded.go_live_active)
        self.assertFalse(svc2.stage5_status(self.admin)["go_live_active"])
        svc2.store.close()

    def test_ordinary_user_denied(self):
        user = SimpleNamespace(actor_ref=lambda: "u", permissions=(), roles=(), tenant_id="t1")
        with self.assertRaises(ProductionActivationError):
            self.svc.stage5_status(user)

    def test_health_when_inactive(self):
        health = self.svc.post_activation_health(self.admin)
        self.assertIn(health["health"], {"DEGRADED", "HEALTHY", "UNHEALTHY"})
        self.assertFalse(health["go_live_active"])


if __name__ == "__main__":
    unittest.main()
