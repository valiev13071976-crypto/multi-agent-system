"""Extended Stage-4 controlled launch tests (policy, admission, gate, kill, containment)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from controlled_launch.access import PERM_LAUNCH_READ, PERM_LAUNCH_WRITE
from controlled_launch.admission import ControlledLaunchAdmission
from controlled_launch.containment import ContainmentEvaluator
from controlled_launch.errors import BLOCKED_BY_STAGE_3, ControlledLaunchError
from controlled_launch.handoff import Stage3HandoffGate
from controlled_launch.models import LaunchEvidence, VerificationClass
from controlled_launch.policy import (
    ControlledLaunchPolicy,
    ControlledLaunchPolicyError,
    activate_policy,
    create_policy,
    pause_policy,
    validate_policy,
    with_kill_switch,
)
from controlled_launch.service import ControlledLaunchService
from controlled_launch.sqlite_store import SqliteControlledLaunchStore
from controlled_launch.stage4_gate import MANDATORY_STAGE4_GATES, Stage4ReleaseGate
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass as PVClass
from production_validation.release_gate import MANDATORY_LIVE_GATES


def _admin():
    return SimpleNamespace(
        actor_ref="ops-admin",
        permissions=(PERM_LAUNCH_READ, PERM_LAUNCH_WRITE, "operations:read", "operations:write"),
        roles=("PLATFORM_ADMIN",),
        tenant_id="platform",
        request_id="req-1",
    )


def _seed_stage3(tmp: str, release_identity: str = "rel-stage4") -> tuple[EvidenceStore, ValidationConfig]:
    store = EvidenceStore(root=tmp)
    config = ValidationConfig(production_url="https://prod.example", release_identity=release_identity, environment="production")
    for gate_name in MANDATORY_LIVE_GATES:
        g = ReleaseEvidence.begin(gate=gate_name, environment="production", mode=ExecutionMode.LIVE_SAFE, release_identity=release_identity)
        g.complete(status=GateStatus.PASS, classification=PVClass.LIVE_VERIFIED.value)
        store.save(g)
    release = ReleaseEvidence.begin(gate="3.17_release_gate", environment="production", mode=ExecutionMode.LIVE_SAFE, release_identity=release_identity)
    release.complete(
        status=GateStatus.PASS,
        classification=PVClass.LIVE_VERIFIED.value,
        safe_metrics={"p0_count": 0, "p1_count": 0, "commit_sha": release_identity, "deployment_id": "dep-1", "blocked_gates": []},
    )
    store.save(release)
    return store, config


class Stage4PrerequisiteAuthoritativeTests(unittest.TestCase):
    def test_mandatory_pass_accepts_even_if_317_metrics_noisy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, config = _seed_stage3(tmp)
            # Simulate local rewrite noise on 3.17 without mandatory failure
            noisy = ReleaseEvidence.begin(gate="3.17_release_gate", environment="production", mode=ExecutionMode.LIVE_SAFE)
            noisy.complete(
                status=GateStatus.BLOCKED,
                classification=PVClass.OPERATOR_ACTION_REQUIRED.value,
                safe_metrics={"blocked_gates": [], "p0_count": 0, "p1_count": 0, "commit_sha": config.release_identity},
            )
            store.save(noisy)
            gate = Stage3HandoffGate(config=config, evidence_store=store)
            handoff = gate.evaluate()
            self.assertEqual(handoff.stage3_status, "CLOSED")
            self.assertEqual(handoff.release_readiness, "READY")
            gate.require_ready()

    def test_missing_mandatory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(root=tmp)
            config = ValidationConfig(production_url="", release_identity="x", environment="production")
            gate = Stage3HandoffGate(config=config, evidence_store=store)
            with self.assertRaises(ControlledLaunchError) as ctx:
                gate.require_ready()
            self.assertEqual(ctx.exception.code, BLOCKED_BY_STAGE_3)


class Stage4PolicyTests(unittest.TestCase):
    def test_valid_bounded_policy(self):
        p = create_policy(release_identity="r1", created_by="ops", tenant_allowlist=["t-a"], max_traffic_percent=5)
        self.assertFalse(p.enabled)
        self.assertEqual(p.max_traffic_percent, 5)
        d = p.as_dict()
        self.assertEqual(ControlledLaunchPolicy.from_dict(d).policy_id, p.policy_id)

    def test_invalid_percentage(self):
        with self.assertRaises(ControlledLaunchPolicyError):
            validate_policy(
                ControlledLaunchPolicy(
                    policy_id="p",
                    policy_version="v",
                    release_identity="r",
                    max_traffic_percent=150,
                )
            )

    def test_invalid_budget(self):
        with self.assertRaises(ControlledLaunchPolicyError):
            validate_policy(
                ControlledLaunchPolicy(
                    policy_id="p",
                    policy_version="v",
                    release_identity="r",
                    budget_ceiling=10,
                    budget_warning_threshold=20,
                )
            )

    def test_missing_release_identity(self):
        with self.assertRaises(ControlledLaunchPolicyError):
            create_policy(release_identity="  ", created_by="ops")

    def test_activate_requires_approval_path(self):
        p = create_policy(release_identity="r1", created_by="ops", tenant_allowlist=["t-a"])
        activated = activate_policy(p, approved_by="ops")
        self.assertTrue(activated.enabled)
        self.assertTrue(activated.operator_approved)


class Stage4AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.adm = ControlledLaunchAdmission()
        self.policy = activate_policy(
            create_policy(
                release_identity="r1",
                created_by="ops",
                tenant_allowlist=["t-a"],
                max_cohort_size=2,
                max_interactive_concurrency=1,
                per_tenant_concurrency=1,
                budget_ceiling=10.0,
                budget_warning_threshold=5.0,
                restricted_capabilities=["payments.refund"],
            ),
            approved_by="ops",
        )

    def test_member_admitted(self):
        d = self.adm.decide(self.policy, tenant_id="t-a", authenticated=True, authorized=True)
        self.assertTrue(d.admitted)
        self.assertEqual(d.reason_code, "admitted")

    def test_non_member_rejected(self):
        d = self.adm.decide(self.policy, tenant_id="t-b", authenticated=True, authorized=True)
        self.assertFalse(d.admitted)
        self.assertEqual(d.reason_code, "not_in_cohort")

    def test_auth_required(self):
        d = self.adm.decide(self.policy, tenant_id="t-a", authenticated=False, authorized=True)
        self.assertEqual(d.reason_code, "unauthenticated")
        d2 = self.adm.decide(self.policy, tenant_id="t-a", authenticated=True, authorized=False)
        self.assertEqual(d2.reason_code, "unauthorized")

    def test_capacity_and_budget(self):
        d = self.adm.decide(self.policy, tenant_id="t-a", authenticated=True, authorized=True, active_interactive=1)
        self.assertEqual(d.reason_code, "interactive_concurrency_limit")
        d2 = self.adm.decide(self.policy, tenant_id="t-a", authenticated=True, authorized=True, spent=10.0)
        self.assertEqual(d2.reason_code, "budget_ceiling")

    def test_capability_narrow_only(self):
        self.assertFalse(self.adm.capability_allowed(self.policy, "payments.refund"))
        self.assertTrue(self.adm.capability_allowed(self.policy, "read.docs"))

    def test_kill_switch(self):
        killed = with_kill_switch(self.policy, enabled=True, actor="ops")
        d = self.adm.decide(killed, tenant_id="t-a", authenticated=True, authorized=True)
        self.assertEqual(d.reason_code, "kill_switch_active")

    def test_tenant_quota_isolation(self):
        d = self.adm.decide(self.policy, tenant_id="t-a", authenticated=True, authorized=True, tenant_active=1)
        self.assertEqual(d.reason_code, "tenant_concurrency_limit")


class Stage4ContainmentTests(unittest.TestCase):
    def test_error_pause(self):
        d = ContainmentEvaluator().evaluate(signals={"error_rate": 0.5}, thresholds={"pause_error_rate": 0.25})
        self.assertEqual(d.action, "PAUSE_ADMISSION")

    def test_security_kill(self):
        d = ContainmentEvaluator().evaluate(signals={"security_failures": 1})
        self.assertEqual(d.action, "KILL_CONTROLLED_LAUNCH")

    def test_continue(self):
        d = ContainmentEvaluator().evaluate(signals={"error_rate": 0.01})
        self.assertEqual(d.action, "CONTINUE")


class Stage4ReleaseGateTests(unittest.TestCase):
    def test_missing_mandatory_blocks(self):
        gate = Stage4ReleaseGate()
        result = gate.evaluate(evidence=[], engineering_pass=True)
        self.assertEqual(result.verdict, "CONTROLLED_LAUNCH_BLOCKED")
        self.assertEqual(result.go_live_eligibility, "NOT_ELIGIBLE")
        self.assertFalse(result.go_live_active)

    def test_all_pass_eligible_not_active(self):
        evidence = [
            LaunchEvidence.create(
                candidate_id="c1",
                environment="production",
                policy_version="v1",
                gate=g,
                status="PASS",
                classification=VerificationClass.CODE_VERIFIED.value,
            )
            for g in MANDATORY_STAGE4_GATES
        ]
        # informational fail must not block
        evidence.append(
            LaunchEvidence.create(
                candidate_id="c1",
                environment="production",
                policy_version="v1",
                gate="4.10_alerting",
                status="FAIL",
                classification=VerificationClass.CODE_VERIFIED.value,
            )
        )
        result = Stage4ReleaseGate().evaluate(evidence=evidence, engineering_pass=True)
        self.assertEqual(result.verdict, "CONTROLLED_LAUNCH_PASS")
        self.assertEqual(result.go_live_eligibility, "ELIGIBLE")
        self.assertFalse(result.go_live_active)

    def test_go_live_active_forbidden(self):
        with self.assertRaises(ControlledLaunchError):
            Stage4ReleaseGate().evaluate(evidence=[], go_live_active=True)


class Stage4ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.ev_root = str(Path(self._tmpdir.name) / "evidence")
        self.ev_store, self.config = _seed_stage3(self.ev_root)
        db = str(Path(self._tmpdir.name) / "launch.sqlite")
        self.store = SqliteControlledLaunchStore(path=db)
        self.svc = ControlledLaunchService(
            store=self.store,
            handoff_gate=Stage3HandoffGate(config=self.config, evidence_store=self.ev_store),
        )
        self.admin = _admin()

    def tearDown(self):
        self.store.close()
        self._tmpdir.cleanup()

    def test_activate_admit_kill_persist_gate(self):
        policy = self.svc.create_launch_policy(
            self.admin,
            release_identity=self.config.release_identity,
            tenant_allowlist=["t-a"],
            max_cohort_size=5,
            max_interactive_concurrency=2,
            budget_ceiling=20.0,
            budget_warning_threshold=10.0,
            restricted_capabilities=["payments.refund"],
            created_by="ops",
        )
        pid = policy["policy_id"]
        activated = self.svc.activate_controlled_launch(self.admin, policy_id=pid)
        self.assertTrue(activated["enabled"])

        ok = self.svc.admit(self.admin, tenant_id="t-a", authenticated=True, authorized=True)
        self.assertTrue(ok["admitted"])
        denied = self.svc.admit(self.admin, tenant_id="t-b", authenticated=True, authorized=True)
        self.assertEqual(denied["reason_code"], "not_in_cohort")

        spend = self.svc.record_spend(12.0)
        self.assertEqual(spend["action"], "WARN")
        spend2 = self.svc.record_spend(10.0)
        self.assertEqual(spend2["action"], "KILL_CONTROLLED_LAUNCH")

        # re-activate path blocked by kill until explicit recovery: create fresh after pause path
        killed = self.svc.kill_controlled_launch(self.admin, policy_id=pid, reason="ops")
        self.assertTrue(killed["kill_switch"])
        blocked = self.svc.admit(self.admin, tenant_id="t-a", authenticated=True, authorized=True)
        self.assertEqual(blocked["reason_code"], "kill_switch_active")

        # persistence across restart
        svc2 = ControlledLaunchService(
            store=SqliteControlledLaunchStore(path=self.store.path),
            handoff_gate=Stage3HandoffGate(config=self.config, evidence_store=self.ev_store),
        )
        loaded = svc2.store.get_launch_policy(pid)
        self.assertTrue(loaded.kill_switch)
        state = svc2.store.get_launch_state("launch")
        self.assertEqual(state["state"], "KILLED")
        self.assertFalse(state.get("go_live_active", True))
        self.assertGreaterEqual(svc2._spent, 20.0)
        svc2.store.close()

        self.svc.seed_stage4_evidence(self.admin, candidate_id="lc-s4", release_identity=self.config.release_identity)
        gate = self.svc.evaluate_stage4_gate(self.admin, candidate_id="lc-s4")
        self.assertEqual(gate["verdict"], "CONTROLLED_LAUNCH_PASS")
        self.assertEqual(gate["go_live_eligibility"], "ELIGIBLE")
        self.assertFalse(gate["go_live_active"])

        status = self.svc.stage4_status(self.admin)
        self.assertFalse(status["go_live_active"])

    def test_idempotent_kill_and_pause(self):
        policy = self.svc.create_launch_policy(
            self.admin,
            release_identity=self.config.release_identity,
            tenant_allowlist=["t-a"],
            created_by="ops",
        )
        pid = policy["policy_id"]
        self.svc.activate_controlled_launch(self.admin, policy_id=pid)
        self.svc.pause_controlled_launch(self.admin, policy_id=pid)
        self.svc.pause_controlled_launch(self.admin, policy_id=pid)
        self.svc.kill_controlled_launch(self.admin, policy_id=pid, reason="r1")
        again = self.svc.kill_controlled_launch(self.admin, policy_id=pid, reason="r2")
        self.assertTrue(again["kill_switch"])
        self.assertFalse(again["enabled"])

    def test_containment_pauses_admission(self):
        policy = self.svc.create_launch_policy(
            self.admin,
            release_identity=self.config.release_identity,
            tenant_allowlist=["t-a"],
            created_by="ops",
        )
        self.svc.activate_controlled_launch(self.admin, policy_id=policy["policy_id"])
        decision = self.svc.evaluate_containment(signals={"error_rate": 0.9})
        self.assertEqual(decision["action"], "PAUSE_ADMISSION")
        denied = self.svc.admit(self.admin, tenant_id="t-a", authenticated=True, authorized=True)
        self.assertIn(denied["reason_code"], {"launch_disabled", "containment_pause_admission", "kill_switch_active"})

    def test_stage3_block_prevents_policy_create(self):
        empty = EvidenceStore(root=str(Path(self._tmpdir.name) / "empty"))
        svc = ControlledLaunchService(
            store=SqliteControlledLaunchStore(),
            handoff_gate=Stage3HandoffGate(config=self.config, evidence_store=empty),
        )
        with self.assertRaises(ControlledLaunchError) as ctx:
            svc.create_launch_policy(self.admin, release_identity="x", tenant_allowlist=["t"], created_by="ops")
        self.assertEqual(ctx.exception.code, BLOCKED_BY_STAGE_3)
        svc.store.close()


if __name__ == "__main__":
    unittest.main()
