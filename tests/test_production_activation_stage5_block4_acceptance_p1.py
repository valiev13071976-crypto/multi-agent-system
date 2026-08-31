"""Stage-5 Block-4 final acceptance P1 patch tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from production_activation.acceptance_evidence import build_live_acceptance_inputs, derive_security_from_hypercare
from production_activation.commands import ActivateProductionCommand, activation_confirmation_token
from production_activation.models import (
    AcceptanceResult,
    ActivationState,
    FinalProductionCandidate,
    GoLivePlan,
    ProductionActivationEvidence,
    Stage5Handoff,
    VerificationClass,
)
from production_activation.plan import GoLivePlanBuilder
from production_activation.service import ProductionActivationService
from production_activation.smoke import REQUIRED_CHECKS
from production_activation.sqlite_store import SqliteProductionActivationStore
from production_activation.handoff import Stage5HandoffGate
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from controlled_launch.handoff import Stage3HandoffGate
from controlled_launch.models import PromotionResult


def _admin(actor: str = "ops"):
    return SimpleNamespace(
        actor_ref=lambda: actor,
        permissions=("operations:activation.read", "operations:activation.write", "operations:activation.authorize", "operations:read"),
        roles=("PLATFORM_ADMIN",),
    )


def _candidate(**kwargs) -> FinalProductionCandidate:
    base = dict(
        candidate_id="stage5-production",
        commit_sha="abc123",
        deployment_id="dep-1",
        environment="production",
        production_url="https://example.test",
        rollback_target="prev",
        stage3_evidence_id="ev3",
        stage4_evidence_id="ev4",
        routing_policy_version="live",
        fingerprint="fp-cand-1",
        backup_state="ready",
    )
    base.update(kwargs)
    return FinalProductionCandidate(**base)


def _plan(candidate: FinalProductionCandidate) -> GoLivePlan:
    return GoLivePlanBuilder.create(
        candidate=candidate,
        authorized_operator="ops",
        monitoring_destination="mon",
        alert_destination="alert",
    )


class Block4AcceptanceP1Tests(unittest.TestCase):
    release = "bf535f7d5c8ce18d9b2dbcf495755dfcc941f738"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmpdir.name) / "pa.sqlite")
        self.store = SqliteProductionActivationStore(path=self.db)
        empty = EvidenceStore(root=str(Path(self._tmpdir.name) / "empty_ev"))
        config = ValidationConfig(production_url="", release_identity="local", environment="local")
        self.svc = ProductionActivationService(
            store=self.store,
            handoff_gate=Stage5HandoffGate(
                stage3_gate=Stage3HandoffGate(config=config, evidence_store=empty, require_stage3_artifact=False),
                require_stage4_artifact=False,
            ),
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
            candidate_id="stage5-production",
            commit_sha="abc123",
            deployment_id="dep-1",
            environment="production",
            rollback_target="prev",
            monitoring_ready=True,
            alerts_ready=True,
            backup_ready=True,
        )
        self.svc.handoff_gate.allows_activation = lambda **kwargs: True  # type: ignore
        self.candidate = _candidate()
        self.plan = _plan(self.candidate)
        self.store.save_candidate(self.candidate)
        self.store.save_plan(self.plan)
        self.svc.create_go_live_policy(_admin(), release_identity=self.release, created_by="ops")
        self.svc.seed_stage5_evidence(_admin(), candidate_id=self.candidate.candidate_id, release_identity=self.release)

    def tearDown(self):
        self.svc.store.close()
        self._tmpdir.cleanup()

    def _activate(self):
        from production_activation.authorization import ActivationAuthorizer

        auth = ActivationAuthorizer().issue(
            candidate=self.candidate,
            plan=self.plan,
            operator_ref="ops",
            idempotency_key="idem-block4",
            release_identity=self.release,
        )
        self.store.save_authorization(auth)
        token = activation_confirmation_token(
            actor_ref="ops",
            candidate_fingerprint=self.candidate.fingerprint,
            deployment_fingerprint=self.candidate.deployment_id,
            plan_fingerprint=self.plan.fingerprint,
        )
        return self.svc.activate(
            _admin(),
            ActivateProductionCommand(
                candidate_id=self.candidate.candidate_id,
                plan_id=self.plan.plan_id,
                authorization_id=auth.authorization_id,
                operator_ref="ops",
                confirmation_token=token,
                idempotency_key="idem-block4",
                expected_policy_version="live",
            ),
        )

    def _live_smoke(self, attempt_id: str, *, release_identity: str | None = None, plan_id: str | None = None):
        observed = {name: "PASS" for name in REQUIRED_CHECKS}
        return self.svc.run_smoke(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            attempt_id=attempt_id,
            mode="live",
            observed=observed,
            plan_id=plan_id or self.plan.plan_id,
            release_identity=release_identity or self.release,
        )

    def _live_hypercare(
        self,
        *,
        requests: int = 10,
        p0_count: int = 0,
        p1_count: int = 0,
        release_identity: str | None = None,
        plan_id: str | None = None,
        classification_pass: bool = True,
    ):
        self.svc.start_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            plan_id=plan_id or self.plan.plan_id,
            release_identity=release_identity or self.release,
            policy={"min_requests": 10, "max_window_seconds": 3600},
        )
        result = self.svc.complete_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            requests=requests,
            p0_count=p0_count,
            p1_count=p1_count,
        )
        if not classification_pass:
            for ev in reversed(self.store.list_evidence(self.candidate.candidate_id)):
                metrics = ev.safe_metrics or {}
                if metrics.get("evidence_kind") == "hypercare":
                    patched = ProductionActivationEvidence(
                        evidence_id=ev.evidence_id,
                        candidate_id=ev.candidate_id,
                        deployment_id=ev.deployment_id,
                        environment=ev.environment,
                        plan_id=ev.plan_id,
                        attempt_id=ev.attempt_id,
                        activation_state=ev.activation_state,
                        acceptance_result=ev.acceptance_result,
                        classification=VerificationClass.CODE_VERIFIED.value,
                        safe_metrics=ev.safe_metrics,
                        recorded_at=ev.recorded_at,
                    )
                    self.store.save_evidence(patched)
                    break
        return result

    def _accept(self):
        return self.svc.evaluate_acceptance(_admin(), candidate_id=self.candidate.candidate_id)

    def _fresh_service(self) -> ProductionActivationService:
        return ProductionActivationService(
            store=SqliteProductionActivationStore(path=self.db),
            handoff_gate=self.svc.handoff_gate,
        )

    def test_a_fresh_process_does_not_trust_default_security_zeros(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        svc2 = self._fresh_service()
        try:
            decision = svc2.evaluate_acceptance(_admin(), candidate_id=self.candidate.candidate_id)
            self.assertEqual(decision["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)
        finally:
            svc2.store.close()

    def test_b_durable_hypercare_p0_p1_consumed(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        hyper = self._live_hypercare(requests=10, p0_count=0, p1_count=0)
        inputs = build_live_acceptance_inputs(
            self.store.list_evidence(self.candidate.candidate_id),
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity=self.release,
            attempt_id=out["attempt"]["attempt_id"],
            candidate=self.candidate,
            hypercare_session=self.store.get_hypercare(self.candidate.candidate_id),
        )
        self.assertEqual(inputs.security_p0, 0)
        self.assertEqual(inputs.security_p1, 0)
        self.assertEqual(hyper["status"], "PASS")
        crafted = ProductionActivationEvidence(
            evidence_id="pa-ev-crafted",
            candidate_id=self.candidate.candidate_id,
            deployment_id="",
            environment="",
            plan_id=self.plan.plan_id,
            attempt_id="",
            activation_state=ActivationState.PRODUCTION_ACTIVE.value,
            acceptance_result=AcceptanceResult.BLOCKED.value,
            classification=VerificationClass.LIVE_VERIFIED.value,
            safe_metrics={
                "evidence_kind": "hypercare",
                "status": "PASS",
                "release_identity": self.release,
                "requests": 10,
                "p0_count": 2,
                "p1_count": 0,
                "policy": {"min_requests": 10},
            },
        )
        p0, p1, reason = derive_security_from_hypercare(crafted, hypercare_session=None)
        self.assertEqual(p0, 2)
        self.assertEqual(reason, "hypercare_security_p0")

    def test_c_hypercare_p0_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare(requests=10, p0_count=1, p1_count=0)
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "live_hypercare_missing_or_unbound")

    def test_d_hypercare_p1_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare(requests=10, p0_count=0, p1_count=1)
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "live_hypercare_missing_or_unbound")

    def test_e_missing_hypercare_metrics_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self.svc.start_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity=self.release,
            policy={"min_requests": 10, "max_window_seconds": 3600},
        )
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertIn(decision["reason"], {"hypercare_metrics_missing", "live_hypercare_missing_or_unbound"})

    def test_f_requests_below_min_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare(requests=9, p0_count=0, p1_count=0)
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "live_hypercare_missing_or_unbound")

    def test_g_non_live_hypercare_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare(classification_pass=False)
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "live_hypercare_missing_or_unbound")

    def test_h_live_verified_hypercare_pass_succeeds(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)

    def test_i_smoke_non_live_blocks(self):
        out = self._activate()
        self.svc.run_smoke(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            attempt_id=out["attempt"]["attempt_id"],
            mode="engineering",
            plan_id=self.plan.plan_id,
            release_identity=self.release,
        )
        self._live_hypercare()
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "live_smoke_missing_or_unbound")

    def test_j_wrong_plan_smoke_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"], plan_id="other-plan")
        self._live_hypercare()
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "live_smoke_missing_or_unbound")

    def test_k_wrong_release_smoke_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"], release_identity="other-release")
        self._live_hypercare()
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "live_smoke_missing_or_unbound")

    def test_l_wrong_plan_hypercare_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare(plan_id="other-plan")
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "live_hypercare_missing_or_unbound")

    def test_m_wrong_release_hypercare_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare(release_identity="other-release")
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "live_hypercare_missing_or_unbound")

    def test_n_recovery_unknown_does_not_pass(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        bad = _candidate(backup_state="unknown")
        self.store.save_candidate(bad)
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "recovery_candidate_backup_not_ready")

    def test_o_valid_durable_recovery_passes(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)

    def test_p_missing_recovery_evidence_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        for ev in list(self.store.list_evidence(self.candidate.candidate_id)):
            metrics = ev.safe_metrics or {}
            if metrics.get("gate") in {"5.11_backup_recovery", "5.12_rollback_readiness"}:
                self.store._conn().execute("DELETE FROM pa_evidence WHERE evidence_id=?", (ev.evidence_id,))
        self.store._conn().commit()
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "recovery_evidence_missing")

    def test_q_recovery_wrong_release_blocks(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        self.svc.seed_stage5_evidence(_admin(), candidate_id=self.candidate.candidate_id, release_identity="wrong-release")
        for ev in self.store.list_evidence(self.candidate.candidate_id):
            metrics = ev.safe_metrics or {}
            if metrics.get("gate") in {"5.11_backup_recovery", "5.12_rollback_readiness"} and metrics.get("release_identity") == self.release:
                self.store._conn().execute("DELETE FROM pa_evidence WHERE evidence_id=?", (ev.evidence_id,))
        self.store._conn().commit()
        decision = self._accept()
        self.assertEqual(decision["result"], AcceptanceResult.BLOCKED.value)
        self.assertEqual(decision["reason"], "recovery_evidence_missing")

    def test_r_live_verified_true_on_success(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        decision = self._accept()
        self.assertTrue(decision["live_verified"])

    def test_s_go_live_pass_true_on_success(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        decision = self._accept()
        self.assertTrue(decision["go_live_pass"])

    def test_t_persists_production_accepted(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        self._accept()
        state = self.store.get_activation_state()
        self.assertEqual(state["acceptance_result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)

    def test_u_fresh_process_reads_terminal_state(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        self._accept()
        svc2 = self._fresh_service()
        try:
            status = svc2.stage5_status(_admin())
            self.assertTrue(status["live_verified"])
            self.assertEqual(status["acceptance_result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)
        finally:
            svc2.store.close()

    def test_v_operator_action_required_false_after_success(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        self._accept()
        status = self.svc.stage5_status(_admin())
        self.assertFalse(status["operator_action_required"])

    def test_w_failed_acceptance_never_live_verified(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare(requests=9)
        decision = self._accept()
        self.assertNotEqual(decision["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)
        self.assertFalse(decision["live_verified"])
        state = self.store.get_activation_state()
        self.assertFalse(state.get("live_verified"))

    def test_x_existing_production_evidence_schema_readable(self):
        out = self._activate()
        smoke = self._live_smoke(out["attempt"]["attempt_id"])
        hyper = self._live_hypercare()
        evidence = self.store.list_evidence(self.candidate.candidate_id)
        self.assertTrue(any(e.evidence_id == smoke["evidence_id"] for e in evidence))
        self.assertTrue(any(e.evidence_id == hyper["evidence_id"] for e in evidence))

    def test_y_duplicate_acceptance_idempotent(self):
        out = self._activate()
        self._live_smoke(out["attempt"]["attempt_id"])
        self._live_hypercare()
        first = self._accept()
        svc2 = self._fresh_service()
        try:
            second = svc2.evaluate_acceptance(_admin(), candidate_id=self.candidate.candidate_id)
            self.assertEqual(first["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)
            self.assertEqual(second["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)
            self.assertTrue(second["live_verified"])
        finally:
            svc2.store.close()


if __name__ == "__main__":
    unittest.main()
