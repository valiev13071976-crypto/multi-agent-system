"""Stage-5 production activation tests."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from controlled_launch.models import CandidateStatus, LaunchCandidate, LaunchEvidence, PromotionResult, RolloutStep, ShadowGateResult, VerificationClass
from controlled_launch.promotion import PromotionGate
from controlled_launch.sqlite_store import SqliteControlledLaunchStore
from production_activation.acceptance import ProductionAcceptanceGate
from production_activation.activation import ProductionTrafficActivator
from production_activation.authorization import ActivationAuthorizer
from production_activation.candidate import FinalCandidateLock, candidate_fingerprint
from production_activation.commands import ActivateProductionCommand, AuthorizeActivationCommand, PrepareActivationCommand, activation_confirmation_token
from production_activation.errors import (
    AUTHORIZATION_DENIED,
    AUTHORIZATION_REPLAY,
    BLOCKED_BY_PREVIOUS_STAGE,
    CANDIDATE_IMMUTABLE,
    CANDIDATE_INVALIDATED,
    ProductionActivationError,
)
from production_activation.handoff import Stage5HandoffGate
from production_activation.models import (
    AcceptanceResult,
    ActivationState,
    FinalProductionCandidate,
    GoLivePlan,
    Stage5Handoff,
    VerificationClass as PAVerificationClass,
)
from production_activation.plan import GoLivePlanBuilder
from production_activation.providers import ProviderManifest
from production_activation.service import ProductionActivationService
from production_activation.side_effects import SideEffectActivationPolicy
from production_activation.smoke import PostLaunchSmokeRunner
from production_activation.sqlite_store import SqliteProductionActivationStore
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence
from production_validation.release_gate import MANDATORY_LIVE_GATES


def _platform_admin():
    return SimpleNamespace(
        actor_ref=lambda: "platform-admin",
        permissions=("operations:activation.read", "operations:activation.write", "operations:activation.authorize", "operations:read"),
        roles=("PLATFORM_ADMIN",),
    )


def _normal_user():
    return SimpleNamespace(actor_ref=lambda: "user", permissions=(), roles=(), tenant_id="t1")


def _final_candidate(**kwargs):
    base = FinalProductionCandidate(
        candidate_id="lc-prod",
        commit_sha="abc123",
        deployment_id="dep-1",
        environment="production",
        production_url="https://prod.example",
        rollback_target="release-prev",
        stage3_evidence_id="ev-s3",
        stage4_evidence_id="ev-s4",
        routing_policy_version="live",
        fingerprint="fp-test",
        backup_state="ready",
    )
    if kwargs:
        c = replace(base, **kwargs)
        fp = candidate_fingerprint(c)
        return replace(c, fingerprint=fp)
    return base


def _plan(candidate: FinalProductionCandidate) -> GoLivePlan:
    return GoLivePlanBuilder.create(
        candidate=candidate,
        authorized_operator="ops",
        monitoring_destination="datadog",
        alert_destination="pagerduty",
    )


class Stage5HandoffTests(unittest.TestCase):
    def test_stage3_open_blocks_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(root=tmp)
            config = ValidationConfig(production_url="", release_identity="x", environment="production")
            from controlled_launch.handoff import Stage3HandoffGate

            gate = Stage5HandoffGate(
                stage3_gate=Stage3HandoffGate(config=config, evidence_store=store, require_stage3_artifact=False),
                require_stage4_artifact=False,
            )
            with self.assertRaises(ProductionActivationError) as ctx:
                gate.require_ready(candidate_id="lc-1")
            self.assertIn(ctx.exception.code, {BLOCKED_BY_PREVIOUS_STAGE, "STAGE5_BLOCKED_BY_STAGE4"})

    def test_go_live_gate_blocked_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(root=tmp)
            config = ValidationConfig(production_url="", release_identity="x", environment="production")
            from controlled_launch.handoff import Stage3HandoffGate

            gate = Stage5HandoffGate(
                stage3_gate=Stage3HandoffGate(config=config, evidence_store=store, require_stage3_artifact=False),
                require_stage4_artifact=False,
            )
            self.assertEqual(gate.go_live_gate_result(candidate_id="lc-1"), "GO_LIVE_BLOCKED")

    def test_stage4_artifact_pass_accepted(self):
        from production_activation.stage4_artifact import load_stage4_handoff_artifact, require_stage4_artifact_ready

        data = load_stage4_handoff_artifact()
        require_stage4_artifact_ready(data)
        self.assertEqual(data["verdict"], "CONTROLLED_LAUNCH_PASS")
        self.assertFalse(data["go_live_active"])


class Stage5CandidateTests(unittest.TestCase):
    def test_candidate_immutable_after_lock(self):
        lock = FinalCandidateLock()
        handoff = Stage5Handoff(
            stage3_status="CLOSED",
            stage3_readiness="READY",
            stage3_p0=0,
            stage3_p1=0,
            stage3_evidence_id="ev3",
            stage4_status="CLOSED",
            promotion_decision=PromotionResult.GO_LIVE_ELIGIBLE.value,
            stage4_p0=0,
            stage4_p1=0,
            stage4_evidence_id="ev4",
            candidate_id="lc-1",
            commit_sha="abc",
            deployment_id="dep",
            environment="production",
            rollback_target="prev",
            monitoring_ready=True,
            alerts_ready=True,
            backup_ready=True,
        )
        lock.handoff_gate.require_ready = lambda **kwargs: handoff  # type: ignore
        locked = lock.lock_from_handoff(handoff, production_url="https://x")
        with self.assertRaises(ProductionActivationError) as ctx:
            lock.assert_immutable(replace(locked, commit_sha="changed"))
        self.assertEqual(ctx.exception.code, CANDIDATE_IMMUTABLE)

    def test_material_change_invalidates(self):
        lock = FinalCandidateLock()
        lock._locked = _final_candidate()
        with self.assertRaises(ProductionActivationError) as ctx:
            lock.invalidate_on_material_change(commit_sha="new", deployment_id="dep-1")
        self.assertEqual(ctx.exception.code, CANDIDATE_INVALIDATED)


class Stage5PlanTests(unittest.TestCase):
    def test_incomplete_plan_rejected(self):
        candidate = _final_candidate()
        with self.assertRaises(ProductionActivationError):
            GoLivePlanBuilder.create(candidate=candidate, authorized_operator="ops", monitoring_destination="", alert_destination="x")

    def test_plan_requires_rollback(self):
        candidate = replace(_final_candidate(), rollback_target="")
        with self.assertRaises(ProductionActivationError):
            GoLivePlanBuilder.create(candidate=candidate, authorized_operator="ops", monitoring_destination="m", alert_destination="a")


class Stage5AuthorizationTests(unittest.TestCase):
    def test_confirmation_binds_candidate(self):
        candidate = _final_candidate()
        plan = _plan(candidate)
        authz = ActivationAuthorizer()
        auth = authz.issue(candidate=candidate, plan=plan, operator_ref="ops", idempotency_key="k1")
        token = activation_confirmation_token(
            actor_ref="ops",
            candidate_fingerprint=candidate.fingerprint,
            deployment_fingerprint=candidate.deployment_id,
            plan_fingerprint=plan.fingerprint,
        )
        authz.verify(
            authorization=auth,
            candidate=candidate,
            plan=plan,
            operator_ref="ops",
            confirmation_token=token,
            idempotency_key="k1",
        )
        wrong = replace(candidate, commit_sha="other", fingerprint=candidate_fingerprint(replace(candidate, commit_sha="other")))
        with self.assertRaises(ProductionActivationError) as ctx:
            authz.verify(
                authorization=auth,
                candidate=wrong,
                plan=plan,
                operator_ref="ops",
                confirmation_token=token,
                idempotency_key="k1",
            )
        self.assertEqual(ctx.exception.code, AUTHORIZATION_DENIED)

    def test_replay_denied(self):
        candidate = _final_candidate()
        plan = _plan(candidate)
        authz = ActivationAuthorizer()
        auth = authz.issue(candidate=candidate, plan=plan, operator_ref="ops", idempotency_key="k1")
        token = activation_confirmation_token(
            actor_ref="ops",
            candidate_fingerprint=candidate.fingerprint,
            deployment_fingerprint=candidate.deployment_id,
            plan_fingerprint=plan.fingerprint,
        )
        authz.verify(authorization=auth, candidate=candidate, plan=plan, operator_ref="ops", confirmation_token=token, idempotency_key="k1")
        authz.consume(auth, idempotency_key="k1")
        with self.assertRaises(ProductionActivationError) as ctx:
            authz.verify(authorization=auth, candidate=candidate, plan=plan, operator_ref="ops", confirmation_token=token, idempotency_key="k1")
        self.assertEqual(ctx.exception.code, AUTHORIZATION_REPLAY)


class Stage5ActivationTests(unittest.TestCase):
    def test_activation_idempotent(self):
        candidate = _final_candidate()
        plan = _plan(candidate)
        activator = ProductionTrafficActivator()
        a1 = activator.activate(candidate=candidate, plan=plan, operator_ref="ops", expected_policy_version="live", idempotency_key="idem-1")
        a2 = activator.activate(candidate=candidate, plan=plan, operator_ref="ops", expected_policy_version="live", idempotency_key="idem-1")
        self.assertEqual(a1.attempt_id, a2.attempt_id)
        self.assertEqual(activator.state, ActivationState.PRODUCTION_ACTIVE.value)

    def test_activation_durable_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "pa.sqlite")
            store = SqliteProductionActivationStore(path=db)
            candidate = _final_candidate()
            store.save_candidate(candidate)
            store.set_activation_state(ActivationState.PRODUCTION_ACTIVE.value, candidate_id=candidate.candidate_id)
            store.close()
            store2 = SqliteProductionActivationStore(path=db)
            state = store2.get_activation_state()
            store2.close()
            self.assertEqual(state["state"], ActivationState.PRODUCTION_ACTIVE.value)
            self.assertEqual(state["candidate_id"], candidate.candidate_id)


class Stage5SideEffectTests(unittest.TestCase):
    def test_traffic_does_not_auto_enable_billing(self):
        policy = SideEffectActivationPolicy.from_plan({"billing": "sandbox"}, billing_mode="sandbox")
        self.assertFalse(policy.billing_live)

    def test_billing_live_requires_authorization(self):
        policy = SideEffectActivationPolicy.from_plan({"billing": "sandbox"}, billing_mode="sandbox")
        with self.assertRaises(ProductionActivationError):
            policy.activate_billing_live(authorized=False, operator_ref="ops")


class Stage5ProviderTests(unittest.TestCase):
    def test_required_provider_blocks_acceptance(self):
        manifest = ProviderManifest.from_plan(("openai",))
        self.assertTrue(manifest.blocks_acceptance())

    def test_optional_not_enabled_does_not_block(self):
        manifest = ProviderManifest.from_plan(required=(), optional=("telegram",))
        manifest.record_not_enabled("telegram")
        self.assertFalse(manifest.blocks_acceptance())


class Stage5SmokeTests(unittest.TestCase):
    def test_critical_smoke_failure(self):
        runner = PostLaunchSmokeRunner()
        result = runner.run(
            candidate_id="lc-1",
            attempt_id="act-1",
            probes={"health": lambda: False},
        )
        self.assertEqual(result["status"], "FAIL")


class Stage5AcceptanceTests(unittest.TestCase):
    def test_acceptance_does_not_require_active_for_blocked(self):
        gate = ProductionAcceptanceGate()
        decision = gate.evaluate(
            candidate_id="lc-1",
            activation_state=ActivationState.GO_LIVE_ELIGIBLE.value,
            smoke_status="PASS",
            hypercare_status="PASS",
            slo_ok=True,
            security_p0=0,
            security_p1=0,
            finops_ok=True,
            runtime_ok=True,
            recovery_ready=True,
            providers_ok=True,
            side_effects_ok=True,
            evidence=[],
        )
        self.assertEqual(decision.result, AcceptanceResult.BLOCKED.value)


class Stage5ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = SqliteProductionActivationStore(path=str(Path(self._tmpdir.name) / "pa.sqlite"))
        empty = EvidenceStore(root=str(Path(self._tmpdir.name) / "empty_ev"))
        config = ValidationConfig(production_url="", release_identity="local", environment="local")
        from controlled_launch.handoff import Stage3HandoffGate

        self.svc = ProductionActivationService(
            store=self.store,
            handoff_gate=Stage5HandoffGate(
                stage3_gate=Stage3HandoffGate(config=config, evidence_store=empty, require_stage3_artifact=False),
                require_stage4_artifact=False,
            ),
        )

    def tearDown(self):
        self.svc.store.close()
        self._tmpdir.cleanup()

    def test_unauthorized_denied(self):
        with self.assertRaises(ProductionActivationError):
            self.svc.preflight(_normal_user(), "lc-1")

    def test_activation_blocked_by_previous_stage(self):
        admin = _platform_admin()
        with self.assertRaises(ProductionActivationError) as ctx:
            self.svc.activate(
                admin,
                ActivateProductionCommand(
                    candidate_id="lc-1",
                    plan_id="p1",
                    authorization_id="a1",
                    operator_ref="ops",
                    confirmation_token="x",
                    idempotency_key="k1",
                ),
            )
        self.assertEqual(ctx.exception.code, BLOCKED_BY_PREVIOUS_STAGE)

    def test_client_cannot_activate(self):
        with self.assertRaises(ProductionActivationError):
            self.svc.reject_client_activation({"activate": True, "candidate_id": "x"})

    def test_full_fixture_activation_flow(self):
        admin = _platform_admin()
        candidate = _final_candidate()
        plan = _plan(candidate)
        self.store.save_candidate(candidate)
        self.store.save_plan(plan)
        authz = ActivationAuthorizer()
        auth = authz.issue(candidate=candidate, plan=plan, operator_ref="ops", idempotency_key="flow-1")
        self.store.save_authorization(auth)
        token = activation_confirmation_token(
            actor_ref="ops",
            candidate_fingerprint=candidate.fingerprint,
            deployment_fingerprint=candidate.deployment_id,
            plan_fingerprint=plan.fingerprint,
        )
        self.svc.handoff_gate.require_ready = lambda **kwargs: Stage5Handoff(  # type: ignore
            stage3_status="CLOSED",
            stage3_readiness="READY",
            stage3_p0=0,
            stage3_p1=0,
            stage3_evidence_id="ev3",
            stage4_status="CLOSED",
            promotion_decision=PromotionResult.GO_LIVE_ELIGIBLE.value,
            stage4_p0=0,
            stage4_p1=0,
            stage4_evidence_id="ev4",
            candidate_id=candidate.candidate_id,
            commit_sha=candidate.commit_sha,
            deployment_id=candidate.deployment_id,
            environment=candidate.environment,
            rollback_target=candidate.rollback_target,
            monitoring_ready=True,
            alerts_ready=True,
            backup_ready=True,
        )
        self.svc.handoff_gate.allows_activation = lambda **kwargs: True  # type: ignore
        out = self.svc.activate(
            admin,
            ActivateProductionCommand(
                candidate_id=candidate.candidate_id,
                plan_id=plan.plan_id,
                authorization_id=auth.authorization_id,
                operator_ref="ops",
                confirmation_token=token,
                idempotency_key="flow-1",
                expected_policy_version="live",
            ),
        )
        self.assertEqual(out["attempt"]["state"], ActivationState.PRODUCTION_ACTIVE.value)
        self.assertFalse(out["live_verified"])
        from production_activation.smoke import REQUIRED_CHECKS

        observed = {name: True for name in REQUIRED_CHECKS}
        smoke = self.svc.run_smoke(
            admin,
            candidate_id=candidate.candidate_id,
            attempt_id=out["attempt"]["attempt_id"],
            mode="live",
            observed=observed,
            plan_id=plan.plan_id,
            release_identity="rel-fixture",
        )
        self.assertEqual(smoke["status"], "PASS")
        self.assertEqual(smoke["classification"], PAVerificationClass.LIVE_VERIFIED.value)
        self.svc.start_hypercare(
            admin,
            candidate_id=candidate.candidate_id,
            plan_id=plan.plan_id,
            release_identity="rel-fixture",
        )
        hyper = self.svc.complete_hypercare(
            admin,
            candidate_id=candidate.candidate_id,
            requests=5,
            p0_count=0,
            p1_count=0,
        )
        self.assertEqual(hyper["status"], "PASS")
        self.svc.providers.record_live("openai")
        self.svc.create_go_live_policy(admin, release_identity="rel-fixture", created_by="ops")
        self.svc.seed_stage5_evidence(
            admin,
            candidate_id=candidate.candidate_id,
            release_identity="rel-fixture",
        )
        acceptance = self.svc.evaluate_acceptance(admin, candidate_id=candidate.candidate_id)
        self.assertEqual(acceptance["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)
        self.assertTrue(acceptance["live_verified"])

    def _svc_hypercare_requests(self):
        if self.svc._hypercare:
            for _ in range(5):
                self.svc._hypercare.record_request()


class Stage5SecurityMatrixTests(unittest.TestCase):
    def test_no_second_router(self):
        svc = ProductionActivationService(store=SqliteProductionActivationStore())
        self.assertIsNotNone(svc.activator.routing_activation)
        self.assertIsInstance(svc.activator, ProductionTrafficActivator)

    def test_promotion_gate_does_not_activate(self):
        from controlled_launch.promotion import PromotionGate

        with self.assertRaises(Exception):
            PromotionGate().forbid_production_activation()


if __name__ == "__main__":
    unittest.main()
