"""Stage-5 final live-evidence / durable activation P1 tests."""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from production_activation.authorization import ActivationAuthorizer
from production_activation.commands import ActivateProductionCommand, activation_confirmation_token
from production_activation.errors import (
    ACTIVATION_CONFLICT,
    ACTIVATION_FAILED,
    AUTHORIZATION_DENIED,
    AUTHORIZATION_REPLAY,
    AUTHORIZATION_STALE,
    ProductionActivationError,
)
from production_activation.handoff import Stage5HandoffGate
from production_activation.models import (
    AcceptanceResult,
    ActivationAuthorization,
    ActivationState,
    FinalProductionCandidate,
    GoLivePlan,
    Stage5Handoff,
    VerificationClass,
)
from production_activation.plan import GoLivePlanBuilder
from production_activation.service import ProductionActivationService
from production_activation.smoke import REQUIRED_CHECKS, PostLaunchSmokeRunner
from production_activation.sqlite_store import SqliteProductionActivationStore
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from controlled_launch.models import PromotionResult


def _admin(actor: str = "ops"):
    return SimpleNamespace(
        actor_ref=lambda: actor,
        permissions=("operations:activation.read", "operations:activation.write", "operations:activation.authorize", "operations:read"),
        roles=("PLATFORM_ADMIN",),
    )


def _candidate(**kwargs) -> FinalProductionCandidate:
    base = dict(
        candidate_id="lc-live-1",
        commit_sha="abc123",
        deployment_id="dep-1",
        environment="production",
        production_url="https://example.test",
        rollback_target="prev",
        stage3_evidence_id="ev3",
        stage4_evidence_id="ev4",
        routing_policy_version="live",
        fingerprint="fp-cand-1",
    )
    base.update(kwargs)
    return FinalProductionCandidate(**base)


def _plan(candidate: FinalProductionCandidate, **kwargs) -> GoLivePlan:
    plan = GoLivePlanBuilder.create(
        candidate=candidate,
        authorized_operator="ops",
        monitoring_destination="mon",
        alert_destination="alert",
    )
    if kwargs:
        data = plan.as_dict()
        data.update(kwargs)
        data["launch_required_providers"] = tuple(data.get("launch_required_providers") or [])
        data["smoke_plan"] = tuple(data.get("smoke_plan") or [])
        data["abort_conditions"] = tuple(data.get("abort_conditions") or [])
        data["rollback_conditions"] = tuple(data.get("rollback_conditions") or [])
        return GoLivePlan(**data)
    return plan


def _ready_handoff(candidate: FinalProductionCandidate) -> Stage5Handoff:
    return Stage5Handoff(
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


class LiveEvidenceP1Tests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmpdir.name) / "pa.sqlite")
        self.store = SqliteProductionActivationStore(path=self.db)
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
        self.svc.handoff_gate.require_ready = lambda **kwargs: _ready_handoff(_candidate())  # type: ignore
        self.svc.handoff_gate.allows_activation = lambda **kwargs: True  # type: ignore
        self.candidate = _candidate()
        self.plan = _plan(self.candidate)
        self.store.save_candidate(self.candidate)
        self.store.save_plan(self.plan)
        self.svc.create_go_live_policy(_admin(), release_identity="rel-live-1", created_by="ops")

    def tearDown(self):
        self.svc.store.close()
        self._tmpdir.cleanup()

    def _issue(self, *, operator="ops", idem="idem-a", ttl=900) -> ActivationAuthorization:
        authz = ActivationAuthorizer(ttl_seconds=ttl)
        auth = authz.issue(
            candidate=self.candidate,
            plan=self.plan,
            operator_ref=operator,
            idempotency_key=idem,
            release_identity="rel-live-1",
        )
        self.store.save_authorization(auth)
        return auth

    def _token(self, auth: ActivationAuthorization | None = None) -> str:
        return activation_confirmation_token(
            actor_ref="ops",
            candidate_fingerprint=self.candidate.fingerprint,
            deployment_fingerprint=self.candidate.deployment_id,
            plan_fingerprint=self.plan.fingerprint,
        )

    def _activate(self, auth, *, idem="idem-a", operator="ops"):
        return self.svc.activate(
            _admin(operator),
            ActivateProductionCommand(
                candidate_id=self.candidate.candidate_id,
                plan_id=self.plan.plan_id,
                authorization_id=auth.authorization_id,
                operator_ref=operator,
                confirmation_token=self._token(auth),
                idempotency_key=idem,
                expected_policy_version="live",
            ),
        )

    def test_a_authorization_durable_across_instances(self):
        auth = self._issue()
        consumed = self.store.consume_authorization(
            auth.authorization_id,
            attempt_id="act-x",
            operator_ref="ops",
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        self.assertTrue(consumed.consumed)
        store2 = SqliteProductionActivationStore(path=self.db)
        loaded = store2.get_authorization(auth.authorization_id)
        store2.close()
        self.assertTrue(loaded.consumed)
        self.assertEqual(loaded.attempt_id, "act-x")

    def test_b_expired_authorization_rejected(self):
        auth = self._issue(ttl=1)
        # Force expiry in stored payload
        expired = ActivationAuthorization(
            **{**auth.as_dict(), "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()}
        )
        self.store.save_authorization(expired)
        with self.assertRaises(ProductionActivationError) as ctx:
            self.store.consume_authorization(
                expired.authorization_id,
                attempt_id="act-x",
                operator_ref="ops",
                candidate_id=self.candidate.candidate_id,
                plan_id=self.plan.plan_id,
            )
        self.assertEqual(ctx.exception.code, AUTHORIZATION_STALE)

    def test_c_wrong_candidate_rejected(self):
        auth = self._issue()
        with self.assertRaises(ProductionActivationError) as ctx:
            self.store.consume_authorization(
                auth.authorization_id,
                attempt_id="act-x",
                operator_ref="ops",
                candidate_id="other-cand",
                plan_id=self.plan.plan_id,
            )
        self.assertEqual(ctx.exception.code, AUTHORIZATION_DENIED)

    def test_d_wrong_plan_rejected(self):
        auth = self._issue()
        with self.assertRaises(ProductionActivationError) as ctx:
            self.store.consume_authorization(
                auth.authorization_id,
                attempt_id="act-x",
                operator_ref="ops",
                candidate_id=self.candidate.candidate_id,
                plan_id="other-plan",
            )
        self.assertEqual(ctx.exception.code, AUTHORIZATION_DENIED)

    def test_e_wrong_operator_rejected(self):
        auth = self._issue()
        with self.assertRaises(ProductionActivationError) as ctx:
            self.store.consume_authorization(
                auth.authorization_id,
                attempt_id="act-x",
                operator_ref="other-ops",
                candidate_id=self.candidate.candidate_id,
                plan_id=self.plan.plan_id,
            )
        self.assertEqual(ctx.exception.code, AUTHORIZATION_DENIED)

    def test_f_double_consumption_fails_closed(self):
        auth = self._issue()
        self.store.consume_authorization(
            auth.authorization_id,
            attempt_id="act-1",
            operator_ref="ops",
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
        )
        with self.assertRaises(ProductionActivationError) as ctx:
            self.store.consume_authorization(
                auth.authorization_id,
                attempt_id="act-2",
                operator_ref="ops",
                candidate_id=self.candidate.candidate_id,
                plan_id=self.plan.plan_id,
            )
        self.assertEqual(ctx.exception.code, AUTHORIZATION_REPLAY)

    def test_g_h_activation_idempotency_across_instances_and_lost_response(self):
        auth = self._issue(idem="idem-retry")
        out1 = self._activate(auth, idem="idem-retry")
        self.assertEqual(out1["attempt"]["state"], ActivationState.PRODUCTION_ACTIVE.value)
        self.assertFalse(out1["live_verified"])
        # New service/store instance simulates new Railway CLI process / lost SSH output
        store2 = SqliteProductionActivationStore(path=self.db)
        empty = EvidenceStore(root=str(Path(self._tmpdir.name) / "empty_ev2"))
        config = ValidationConfig(production_url="", release_identity="local", environment="local")
        from controlled_launch.handoff import Stage3HandoffGate

        svc2 = ProductionActivationService(
            store=store2,
            handoff_gate=Stage5HandoffGate(
                stage3_gate=Stage3HandoffGate(config=config, evidence_store=empty, require_stage3_artifact=False),
                require_stage4_artifact=False,
            ),
        )
        svc2.handoff_gate.allows_activation = lambda **kwargs: True  # type: ignore
        out2 = svc2.activate(
            _admin(),
            ActivateProductionCommand(
                candidate_id=self.candidate.candidate_id,
                plan_id=self.plan.plan_id,
                authorization_id=auth.authorization_id,
                operator_ref="ops",
                confirmation_token=self._token(auth),
                idempotency_key="idem-retry",
                expected_policy_version="live",
            ),
        )
        self.assertTrue(out2["already_applied"])
        self.assertEqual(out1["attempt"]["attempt_id"], out2["attempt"]["attempt_id"])
        svc2.store.close()

    def test_i_concurrent_duplicate_activation(self):
        auth = self._issue(idem="idem-race")
        results = []
        errors = []

        def worker():
            try:
                store = SqliteProductionActivationStore(path=self.db)
                empty = EvidenceStore(root=str(Path(self._tmpdir.name) / f"ev-{threading.get_ident()}"))
                config = ValidationConfig(production_url="", release_identity="local", environment="local")
                from controlled_launch.handoff import Stage3HandoffGate

                svc = ProductionActivationService(
                    store=store,
                    handoff_gate=Stage5HandoffGate(
                        stage3_gate=Stage3HandoffGate(config=config, evidence_store=empty, require_stage3_artifact=False),
                        require_stage4_artifact=False,
                    ),
                )
                svc.handoff_gate.allows_activation = lambda **kwargs: True  # type: ignore
                out = svc.activate(
                    _admin(),
                    ActivateProductionCommand(
                        candidate_id=self.candidate.candidate_id,
                        plan_id=self.plan.plan_id,
                        authorization_id=auth.authorization_id,
                        operator_ref="ops",
                        confirmation_token=self._token(auth),
                        idempotency_key="idem-race",
                        expected_policy_version="live",
                    ),
                )
                results.append(out)
                store.close()
            except ProductionActivationError as exc:
                errors.append(exc.code)
            except Exception as exc:  # pragma: no cover
                errors.append(type(exc).__name__)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        successes = [r for r in results if r.get("attempt", {}).get("state") == ActivationState.PRODUCTION_ACTIVE.value]
        self.assertGreaterEqual(len(successes), 1)
        # At most one non-already_applied success; others replay/conflict/already_applied
        fresh = [r for r in successes if not r.get("already_applied")]
        self.assertLessEqual(len(fresh), 1)
        attempt_ids = {r["attempt"]["attempt_id"] for r in successes}
        self.assertEqual(len(attempt_ids), 1)

    def test_j_missing_live_probes_cannot_pass(self):
        runner = PostLaunchSmokeRunner()
        result = runner.run(
            candidate_id="c1",
            attempt_id="a1",
            mode="live",
            observed={"health": True},
            plan_id="p1",
            release_identity="rel-1",
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertNotEqual(result["classification"], VerificationClass.LIVE_VERIFIED.value)
        self.assertIn("readiness", result["missing_probes"])

    def test_k_code_verified_cannot_satisfy_live_acceptance(self):
        auth = self._issue(idem="idem-cv")
        out = self._activate(auth, idem="idem-cv")
        self.svc.run_smoke(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            attempt_id=out["attempt"]["attempt_id"],
            mode="engineering",
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        self.svc.start_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        self.svc.complete_hypercare(_admin(), candidate_id=self.candidate.candidate_id, requests=5, p0_count=0, p1_count=0)
        self.svc.providers.record_live("openai")
        self.svc.recovery.persistent_db = "ready"
        self.svc.recovery.workflow = "ready"
        self.svc.recovery.audit = "ready"
        self.svc.recovery.stage3_restore_reusable = True
        decision = self.svc.evaluate_acceptance(_admin(), candidate_id=self.candidate.candidate_id)
        self.assertNotEqual(decision["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)
        self.assertFalse(decision["live_verified"])

    def test_l_m_n_o_live_smoke_persisted_and_bound(self):
        auth = self._issue(idem="idem-smoke")
        out = self._activate(auth, idem="idem-smoke")
        observed = {name: "PASS" for name in REQUIRED_CHECKS}
        smoke = self.svc.run_smoke(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            attempt_id=out["attempt"]["attempt_id"],
            mode="live",
            observed=observed,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        self.assertEqual(smoke["status"], "PASS")
        self.assertEqual(smoke["classification"], VerificationClass.LIVE_VERIFIED.value)
        ev = next(e for e in self.store.list_evidence(self.candidate.candidate_id) if e.evidence_id == smoke["evidence_id"])
        self.assertEqual(ev.candidate_id, self.candidate.candidate_id)
        self.assertEqual(ev.plan_id, self.plan.plan_id)
        self.assertEqual(ev.safe_metrics["release_identity"], "rel-live-1")

    def test_p_critical_smoke_failure_blocks_acceptance(self):
        auth = self._issue(idem="idem-crit")
        out = self._activate(auth, idem="idem-crit")
        observed = {name: "PASS" for name in REQUIRED_CHECKS}
        observed["health"] = "FAIL"
        smoke = self.svc.run_smoke(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            attempt_id=out["attempt"]["attempt_id"],
            mode="live",
            observed=observed,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        self.assertEqual(smoke["status"], "FAIL")
        self.svc.start_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        self.svc.complete_hypercare(_admin(), candidate_id=self.candidate.candidate_id, requests=5, p0_count=0, p1_count=0)
        self.svc.providers.record_live("openai")
        self.svc.recovery.persistent_db = "ready"
        self.svc.recovery.workflow = "ready"
        self.svc.recovery.audit = "ready"
        self.svc.recovery.stage3_restore_reusable = True
        decision = self.svc.evaluate_acceptance(_admin(), candidate_id=self.candidate.candidate_id)
        self.assertNotEqual(decision["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)

    def test_q_r_hypercare_durable_across_instances(self):
        self.svc.start_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        store2 = SqliteProductionActivationStore(path=self.db)
        empty = EvidenceStore(root=str(Path(self._tmpdir.name) / "empty_ev3"))
        config = ValidationConfig(production_url="", release_identity="local", environment="local")
        from controlled_launch.handoff import Stage3HandoffGate

        svc2 = ProductionActivationService(
            store=store2,
            handoff_gate=Stage5HandoffGate(
                stage3_gate=Stage3HandoffGate(config=config, evidence_store=empty, require_stage3_artifact=False),
                require_stage4_artifact=False,
            ),
        )
        session = store2.get_hypercare(self.candidate.candidate_id)
        self.assertIsNotNone(session)
        self.assertEqual(session["status"], "RUNNING")
        result = svc2.complete_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            requests=3,
            p0_count=0,
            p1_count=0,
        )
        self.assertEqual(result["status"], "PASS")
        store3 = SqliteProductionActivationStore(path=self.db)
        done = store3.get_hypercare(self.candidate.candidate_id)
        store3.close()
        svc2.store.close()
        self.assertEqual(done["status"], "PASS")

    def test_s_missing_hypercare_metrics_fail_closed(self):
        self.svc.start_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        result = self.svc.complete_hypercare(_admin(), candidate_id=self.candidate.candidate_id, require_metrics=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result.get("reason"), "missing_hypercare_metrics")

    def test_t_stale_hypercare_cannot_satisfy_acceptance(self):
        auth = self._issue(idem="idem-stale")
        out = self._activate(auth, idem="idem-stale")
        observed = {name: "PASS" for name in REQUIRED_CHECKS}
        self.svc.run_smoke(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            attempt_id=out["attempt"]["attempt_id"],
            mode="live",
            observed=observed,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        # Hypercare bound to wrong release
        self.svc.start_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity="other-release",
        )
        self.svc.complete_hypercare(_admin(), candidate_id=self.candidate.candidate_id, requests=5, p0_count=0, p1_count=0)
        self.svc.providers.record_live("openai")
        self.svc.recovery.persistent_db = "ready"
        self.svc.recovery.workflow = "ready"
        self.svc.recovery.audit = "ready"
        self.svc.recovery.stage3_restore_reusable = True
        decision = self.svc.evaluate_acceptance(_admin(), candidate_id=self.candidate.candidate_id)
        self.assertNotEqual(decision["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)

    def test_u_v_w_full_lifecycle_live_verified(self):
        auth = self._issue(idem="idem-full")
        out = self._activate(auth, idem="idem-full")
        self.assertFalse(out["live_verified"])
        self.assertTrue(out["go_live_active"])
        observed = {name: True for name in REQUIRED_CHECKS}
        smoke = self.svc.run_smoke(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            attempt_id=out["attempt"]["attempt_id"],
            mode="live",
            observed=observed,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        self.assertEqual(smoke["classification"], VerificationClass.LIVE_VERIFIED.value)
        self.svc.start_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        hyper = self.svc.complete_hypercare(
            _admin(), candidate_id=self.candidate.candidate_id, requests=5, p0_count=0, p1_count=0
        )
        self.assertEqual(hyper["status"], "PASS")
        self.svc.providers.record_live("openai")
        self.svc.recovery.persistent_db = "ready"
        self.svc.recovery.workflow = "ready"
        self.svc.recovery.audit = "ready"
        self.svc.recovery.stage3_restore_reusable = True
        decision = self.svc.evaluate_acceptance(_admin(), candidate_id=self.candidate.candidate_id)
        self.assertEqual(decision["result"], AcceptanceResult.PRODUCTION_ACCEPTED.value)
        self.assertTrue(decision["live_verified"])
        self.assertTrue(decision["go_live_pass"])
        status = self.svc.stage5_status(_admin())
        self.assertTrue(status["live_verified"])
        self.assertTrue(status["go_live_active"])

    def test_x_deactivate_clears_live_truth(self):
        auth = self._issue(idem="idem-rb")
        out = self._activate(auth, idem="idem-rb")
        observed = {name: True for name in REQUIRED_CHECKS}
        self.svc.run_smoke(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            attempt_id=out["attempt"]["attempt_id"],
            mode="live",
            observed=observed,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        self.svc.start_hypercare(
            _admin(),
            candidate_id=self.candidate.candidate_id,
            plan_id=self.plan.plan_id,
            release_identity="rel-live-1",
        )
        self.svc.complete_hypercare(_admin(), candidate_id=self.candidate.candidate_id, requests=5, p0_count=0, p1_count=0)
        self.svc.providers.record_live("openai")
        self.svc.recovery.persistent_db = "ready"
        self.svc.recovery.workflow = "ready"
        self.svc.recovery.audit = "ready"
        self.svc.recovery.stage3_restore_reusable = True
        self.svc.evaluate_acceptance(_admin(), candidate_id=self.candidate.candidate_id)
        self.svc.deactivate(_admin(), candidate_id=self.candidate.candidate_id, operator_ref="ops", reason="test")
        status = self.svc.stage5_status(_admin())
        self.assertFalse(status["go_live_active"])
        self.assertFalse(status["live_verified"])
        decision = self.svc.evaluate_acceptance(_admin(), candidate_id=self.candidate.candidate_id)
        self.assertEqual(decision["result"], AcceptanceResult.ROLLED_BACK.value)

    def test_y_status_recovery_after_lost_output(self):
        auth = self._issue(idem="idem-status")
        out = self._activate(auth, idem="idem-status")
        status = self.svc.stage5_status(_admin())
        self.assertEqual(status["attempt"]["attempt_id"], out["attempt"]["attempt_id"])
        self.assertTrue(status["authorization"]["consumed"])
        self.assertNotIn("confirmation_token", status["authorization"])
        self.assertEqual(status["attempt"]["idempotency_key"], "idem-status")
        self.assertTrue(status["go_live_active"])
        self.assertFalse(status["live_verified"])


if __name__ == "__main__":
    unittest.main()
