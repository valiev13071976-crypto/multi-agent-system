"""Stage-4 controlled launch tests."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from controlled_launch.access import PERM_LAUNCH_READ, PERM_LAUNCH_WRITE
from controlled_launch.candidate import LaunchCandidateManager
from controlled_launch.commands import AbortRolloutCommand, HoldRolloutCommand, RollbackRolloutCommand, StartInternalCommand
from controlled_launch.errors import (
    BLOCKED_BY_STAGE_3,
    CANDIDATE_IMMUTABLE,
    PRODUCTION_ACTIVE_FORBIDDEN,
    SHADOW_SIDE_EFFECT_DENIED,
    ControlledLaunchError,
)
from controlled_launch.handoff import Stage3HandoffGate
from controlled_launch.models import CandidateStatus, LaunchCandidate, RolloutStep, ShadowGateResult, TrafficMode
from controlled_launch.promotion import PromotionGate
from controlled_launch.rollout import RolloutManager
from controlled_launch.router import ControlledLaunchRouter
from controlled_launch.service import ControlledLaunchService
from controlled_launch.shadow import ShadowController
from controlled_launch.side_effect_policy import ShadowSideEffectPolicy, SideEffectOwnershipPolicy
from controlled_launch.sqlite_store import SqliteControlledLaunchStore
from controlled_launch.traffic_policy import CohortResolver, TrafficPolicyFactory, stable_bucket
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass
from production_validation.release_gate import MANDATORY_LIVE_GATES, ReleaseGateEvaluator


def _admin(perms=None, roles=None):
    return SimpleNamespace(
        actor_ref=lambda: "ops-admin",
        permissions=tuple(perms or (PERM_LAUNCH_READ, PERM_LAUNCH_WRITE, "operations:read", "operations:write")),
        roles=tuple(roles or ("PLATFORM_ADMIN",)),
        tenant_id="platform",
        request_id="req-1",
    )


def _normal_user():
    return SimpleNamespace(actor_ref=lambda: "user-1", permissions=(), roles=(), tenant_id="t1", request_id="req-2")


def _candidate(**kwargs):
    base = LaunchCandidate(
        candidate_id="lc-test",
        commit_sha="abc123",
        deployment_id="dep-1",
        environment="production",
        production_url="https://example.com",
        rollback_target="release-prev",
        stage3_evidence_id="ev-s3",
        status=CandidateStatus.LOCKED.value,
    )
    return replace(base, **kwargs) if kwargs else base


class Stage4HandoffTests(unittest.TestCase):
    def test_stage3_open_blocks_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(root=tmp)
            config = ValidationConfig(production_url="", release_identity="abc", environment="production")
            gate = Stage3HandoffGate(config=config, evidence_store=store)
            with self.assertRaises(ControlledLaunchError) as ctx:
                gate.require_ready()
            self.assertEqual(ctx.exception.code, BLOCKED_BY_STAGE_3)

    def test_stale_handoff_rejects_commit_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(root=tmp)
            config = ValidationConfig(production_url="https://prod.example", release_identity="abc123", environment="production")
            ev = ReleaseEvidence.begin(gate="3.17_release_gate", environment="production", mode=ExecutionMode.LIVE_SAFE, release_identity="abc123")
            ev.complete(status=GateStatus.PASS, classification=VerificationClass.LIVE_VERIFIED.value, safe_metrics={"p0_count": 0, "p1_count": 0, "deployment_id": "dep-1"})
            store.save(ev)
            for gate_name in MANDATORY_LIVE_GATES:
                g = ReleaseEvidence.begin(gate=gate_name, environment="production", mode=ExecutionMode.LIVE_SAFE)
                g.complete(status=GateStatus.PASS, classification=VerificationClass.LIVE_VERIFIED.value)
                store.save(g)
            gate = Stage3HandoffGate(config=config, evidence_store=store)
            handoff = gate.evaluate()
            self.assertEqual(handoff.release_readiness, "READY")
            with self.assertRaises(ControlledLaunchError) as ctx:
                gate.require_ready(commit_sha="different")
            self.assertEqual(ctx.exception.code, "STALE_STAGE3_HANDOFF")


class Stage4CandidateTests(unittest.TestCase):
    def test_candidate_immutable_after_lock(self):
        mgr = LaunchCandidateManager()
        draft = mgr.draft(
            commit_sha="a",
            deployment_id="d",
            environment="local",
            production_url="https://x",
            rollback_target="prev",
            created_by="ops",
            require_stage3=False,
        )
        locked = mgr.lock(draft, actor="ops")
        with self.assertRaises(ControlledLaunchError) as ctx:
            mgr.assert_immutable_identity(locked, replace(locked, commit_sha="changed"))
        self.assertEqual(ctx.exception.code, CANDIDATE_IMMUTABLE)

    def test_rollback_target_required(self):
        mgr = LaunchCandidateManager()
        with self.assertRaises(ControlledLaunchError):
            mgr.draft(
                commit_sha="a",
                deployment_id="d",
                environment="local",
                production_url="https://x",
                rollback_target="",
                created_by="ops",
                require_stage3=False,
            )


class Stage4TrafficPolicyTests(unittest.TestCase):
    def test_client_cannot_choose_mode(self):
        router = ControlledLaunchRouter()
        with self.assertRaises(ControlledLaunchError):
            router.reject_client_authority({"mode": "CANARY"})

    def test_deterministic_assignment(self):
        policy = TrafficPolicyFactory.create(
            candidate_id="lc-1",
            mode=TrafficMode.CANARY,
            created_by="ops",
            percent_basis_points=5000,
        )
        d1 = CohortResolver.resolve(policy, request_id="r1", tenant_id="t1", user_id="u1")
        d2 = CohortResolver.resolve(policy, request_id="r1", tenant_id="t1", user_id="u1")
        self.assertEqual(d1.cohort_id, d2.cohort_id)
        self.assertEqual(d1.mode, d2.mode)

    def test_exclusion_overrides_percentage(self):
        policy = TrafficPolicyFactory.create(
            candidate_id="lc-1",
            mode=TrafficMode.CANARY,
            created_by="ops",
            percent_basis_points=10000,
            excluded_tenants=frozenset({"sensitive"}),
        )
        decision = CohortResolver.resolve(policy, request_id="r1", tenant_id="sensitive", user_id="u1")
        self.assertEqual(decision.mode, TrafficMode.CONTROL.value)
        self.assertEqual(decision.assignment_reason, "exclusion")

    def test_internal_tenant_priority(self):
        policy = TrafficPolicyFactory.create(
            candidate_id="lc-1",
            mode=TrafficMode.INTERNAL,
            created_by="ops",
            internal_tenants=frozenset({"internal-ops"}),
        )
        decision = CohortResolver.resolve(policy, request_id="r1", tenant_id="internal-ops")
        self.assertEqual(decision.mode, TrafficMode.INTERNAL.value)

    def test_stable_bucket(self):
        a = stable_bucket(identity_key="t:u", candidate_id="c", policy_version="v1")
        b = stable_bucket(identity_key="t:u", candidate_id="c", policy_version="v1")
        self.assertEqual(a, b)


class Stage4ShadowTests(unittest.TestCase):
    def test_shadow_side_effect_firewall(self):
        for effect in ("payment", "email_send", "telegram_send", "crm_write", "bitrix_write", "onec_write", "order_write", "price_write", "stock_write"):
            with self.subTest(effect=effect):
                with self.assertRaises(ControlledLaunchError) as ctx:
                    ShadowSideEffectPolicy.authorize(mode="SHADOW", side_effect_type=effect, candidate_target=True, shadow_path=True)
                self.assertEqual(ctx.exception.code, SHADOW_SIDE_EFFECT_DENIED)

    def test_shadow_failure_does_not_block_control(self):
        shadow = ShadowController(max_concurrency=0, max_requests=0)
        evidence, status = shadow.execute(candidate_id="lc-1", tenant_id="t1", input_ref="in-1")
        self.assertIsNone(evidence)
        self.assertEqual(status, "SHADOW_SKIPPED_CAPACITY")

    def test_shadow_gate_zero_tolerance(self):
        shadow = ShadowController()
        shadow.metrics.requests = 10
        result, ev = shadow.evaluate_gate(
            candidate_id="lc-1",
            environment="prod",
            policy_version="v1",
            security_events=["cross_tenant_exposure"],
        )
        self.assertEqual(result, ShadowGateResult.SHADOW_FAIL)
        self.assertNotIn("secret", str(ev.safe_metrics))


class Stage4CanaryRolloutTests(unittest.TestCase):
    def test_canary_requires_shadow_pass(self):
        from controlled_launch.canary import CanaryControllerService
        from controlled_launch.models import CanaryPlan

        svc = CanaryControllerService()
        plan = CanaryPlan(
            plan_id="p1",
            candidate_id="lc-1",
            control_release="prev",
            cohort="initial",
            traffic_allocation_basis_points=100,
            max_concurrency=1,
            max_requests=10,
            max_duration_seconds=60,
            max_cost=1.0,
            observation_duration_seconds=30,
            guardrails={},
            rollback_target="prev",
            side_effect_policy="sandbox",
            billing_policy="sandbox",
            provider_policy="default",
            approved_by="ops",
        )
        with self.assertRaises(ControlledLaunchError):
            svc.prepare(candidate=_candidate(status=CandidateStatus.LOCKED.value), plan=plan, shadow_gate=ShadowGateResult.SHADOW_FAIL)

    def test_hold_prevents_expansion(self):
        mgr = RolloutManager(candidate_id="lc-1")
        mgr.hold()
        with self.assertRaises(ControlledLaunchError):
            mgr.complete_current()

    def test_abort_stops_advance(self):
        mgr = RolloutManager(candidate_id="lc-1")
        mgr.abort()
        with self.assertRaises(ControlledLaunchError):
            mgr.advance_to(RolloutStep.SHADOW)

    def test_rollout_cannot_skip_steps(self):
        mgr = RolloutManager(candidate_id="lc-1")
        with self.assertRaises(ControlledLaunchError):
            mgr.advance_to(RolloutStep.INITIAL_CANARY)


class Stage4SideEffectTests(unittest.TestCase):
    def test_no_duplicate_owner(self):
        with self.assertRaises(ControlledLaunchError):
            SideEffectOwnershipPolicy.owner_for_decision(control_target=True, candidate_target=True, logical_operation_id="op-1")

    def test_control_owner(self):
        owner = SideEffectOwnershipPolicy.owner_for_decision(control_target=True, candidate_target=False, logical_operation_id="op-1")
        self.assertTrue(owner.startswith("control:"))


class Stage4PromotionTests(unittest.TestCase):
    def test_eligible_not_active(self):
        gate = PromotionGate()
        with self.assertRaises(ControlledLaunchError) as ctx:
            gate.forbid_production_activation()
        self.assertEqual(ctx.exception.code, PRODUCTION_ACTIVE_FORBIDDEN)

    def test_promotion_blocked_without_evidence(self):
        gate = PromotionGate()
        decision = gate.evaluate(candidate_id="lc-1", candidate_status=CandidateStatus.LOCKED.value, evidence=[], stage3_ready=True)
        self.assertEqual(decision.result, "GO_LIVE_BLOCKED")

    def test_service_has_no_activate_full_production(self):
        svc = ControlledLaunchService(store=SqliteControlledLaunchStore())
        with self.assertRaises(ControlledLaunchError):
            svc.activate_full_production()


class Stage4ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "launch.sqlite")
        self.store = SqliteControlledLaunchStore(path=db_path)
        self.svc = ControlledLaunchService(store=self.store)

    def tearDown(self):
        self.svc.store.close()
        self._tmpdir.cleanup()

    def test_durability_restart(self):
        admin = _admin()
        created = self.svc.create_candidate(
            admin,
            commit_sha="abc",
            deployment_id="dep",
            environment="local",
            production_url="https://x",
            rollback_target="prev",
            created_by="ops",
        )
        cid = created["candidate_id"]
        self.svc.lock_candidate(admin, cid)
        store2 = SqliteControlledLaunchStore(path=self.store.path)
        svc2 = ControlledLaunchService(store=store2)
        loaded = svc2.store.get_candidate(cid)
        svc2.store.close()
        self.assertEqual(loaded.status, CandidateStatus.LOCKED.value)

    def test_unauthorized_denied(self):
        with self.assertRaises(ControlledLaunchError):
            self.svc.read_model(_normal_user(), "missing")

    def test_live_internal_blocked_by_stage3(self):
        admin = _admin()
        created = self.svc.create_candidate(
            admin,
            commit_sha="abc",
            deployment_id="dep",
            environment="local",
            production_url="https://x",
            rollback_target="prev",
            created_by="ops",
        )
        self.svc.lock_candidate(admin, created["candidate_id"])
        with self.assertRaises(ControlledLaunchError) as ctx:
            self.svc.start_internal(admin, StartInternalCommand(candidate_id=created["candidate_id"], actor_ref="ops"))
        self.assertEqual(ctx.exception.code, BLOCKED_BY_STAGE_3)

    def test_hold_abort_rollback_audited(self):
        admin = _admin()
        created = self.svc.create_candidate(
            admin,
            commit_sha="abc",
            deployment_id="dep",
            environment="local",
            production_url="https://x",
            rollback_target="prev",
            created_by="ops",
        )
        cid = created["candidate_id"]
        self.svc.lock_candidate(admin, cid)
        self.svc.hold(admin, HoldRolloutCommand(candidate_id=cid, actor_ref="ops", reason="test"))
        self.svc.abort(admin, AbortRolloutCommand(candidate_id=cid, actor_ref="ops", reason="test"))
        self.svc.rollback(admin, RollbackRolloutCommand(candidate_id=cid, actor_ref="ops", reason="test"))
        audit = self.store.list_audit(candidate_id=cid)
        actions = {a["action"] for a in audit}
        self.assertTrue({"hold", "abort", "rollback"}.issubset(actions))

    def test_idempotent_rollback(self):
        admin = _admin()
        created = self.svc.create_candidate(
            admin,
            commit_sha="abc",
            deployment_id="dep",
            environment="local",
            production_url="https://x",
            rollback_target="prev",
            created_by="ops",
        )
        cid = created["candidate_id"]
        self.svc.lock_candidate(admin, cid)
        cmd = RollbackRolloutCommand(candidate_id=cid, actor_ref="ops")
        self.svc.rollback(admin, cmd)
        self.svc.rollback(admin, cmd)
        candidate = self.store.get_candidate(cid)
        self.assertEqual(candidate.status, CandidateStatus.ROLLED_BACK.value)


class Stage4SecurityMatrixTests(unittest.TestCase):
    """Spot-check mandatory security matrix items."""

    def test_matrix_client_traffic_authority(self):
        router = ControlledLaunchRouter()
        for key in ("candidate_id", "percent", "side_effect_allowed", "billing_allowed"):
            with self.subTest(key=key):
                with self.assertRaises(ControlledLaunchError):
                    router.reject_client_authority({key: "x"})

    def test_matrix_shadow_gate_blocks_canary(self):
        from controlled_launch.canary import CanaryControllerService
        from controlled_launch.models import CanaryPlan

        svc = CanaryControllerService()
        plan = CanaryPlan(
            plan_id="p1",
            candidate_id="lc-1",
            control_release="prev",
            cohort="initial",
            traffic_allocation_basis_points=100,
            max_concurrency=1,
            max_requests=10,
            max_duration_seconds=60,
            max_cost=1.0,
            observation_duration_seconds=30,
            guardrails={},
            rollback_target="prev",
            side_effect_policy="sandbox",
            billing_policy="sandbox",
            provider_policy="default",
            approved_by="ops",
        )
        with self.assertRaises(ControlledLaunchError):
            svc.prepare(candidate=_candidate(), plan=plan, shadow_gate=ShadowGateResult.SHADOW_FAIL)

    def test_matrix_no_second_router_activation(self):
        svc = ControlledLaunchService(store=SqliteControlledLaunchStore())
        self.assertIsNotNone(svc.activation)
        self.assertFalse(hasattr(svc.promotion_gate, "activate_production"))


if __name__ == "__main__":
    unittest.main()
