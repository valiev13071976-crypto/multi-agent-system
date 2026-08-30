"""FH.16 — Evals → Router governed activation (eligible ≠ active)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from evals.activation import ActivationError, RoutingActivationService
from evals.promotion import (
    STAGE_CANARY_VALIDATED,
    STAGE_PRODUCTION_ELIGIBLE,
    CandidatePolicy,
    PromotionGovernor,
)
from evals.versions import ROUTING_POLICY_VERSION


T0 = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _eligible(**kw) -> CandidatePolicy:
    base = dict(
        candidate_id="cand-1",
        candidate_version="1",
        base_routing_policy_version=ROUTING_POLICY_VERSION,
        proposed_routing_policy_version=ROUTING_POLICY_VERSION,
        stage=STAGE_PRODUCTION_ELIGIBLE,
        eval_suite_id="suite",
        eval_suite_version="1",
        eval_run_id="run-1",
        eval_manifest_hash="hash",
        model_profile_version=ROUTING_POLICY_VERSION,
        provider_profile_versions={},
        eval_artifact_ref="art",
        shadow_evidence_id="sh",
        canary_evidence_id="ca",
        release_gate_decision="PASS",
        release_gate_reason_codes=(),
        production_eligible=True,
        production_active=False,
        created_at=T0,
        updated_at=T0,
        transitions=(),
    )
    base.update(kw)
    return CandidatePolicy(**base)


class FHRoutingActivationTests(unittest.TestCase):
    def setUp(self):
        self.svc = RoutingActivationService(max_candidate_age=timedelta(days=30))

    def test_eligible_not_active_by_default(self):
        cand = _eligible()
        self.assertTrue(cand.production_eligible)
        self.assertFalse(cand.production_active)
        self.assertIsNone(self.svc.get_active())
        self.assertFalse(hasattr(PromotionGovernor, "activate_production"))

    def test_explicit_activation(self):
        rec = self.svc.activate(
            _eligible(),
            actor_ref="ops:alice",
            expected_policy_version=ROUTING_POLICY_VERSION,
            now=T0,
        )
        self.assertEqual(rec.candidate_id, "cand-1")
        self.assertEqual(self.svc.active_policy_version, ROUTING_POLICY_VERSION)
        self.assertIsNotNone(self.svc.get_active())

    def test_version_mismatch_reject(self):
        with self.assertRaises(ActivationError) as ctx:
            self.svc.activate(
                _eligible(),
                actor_ref="ops:alice",
                expected_policy_version="9.9.9-not-real",
                now=T0,
            )
        self.assertIn("mismatch", ctx.exception.reason_code)

    def test_stale_candidate_reject(self):
        old = _eligible(updated_at=T0 - timedelta(days=60))
        with self.assertRaises(ActivationError) as ctx:
            self.svc.activate(
                old,
                actor_ref="ops:alice",
                expected_policy_version=ROUTING_POLICY_VERSION,
                now=T0,
            )
        self.assertEqual(ctx.exception.reason_code, "candidate_stale")

    def test_non_eligible_stage_reject(self):
        cand = _eligible(stage=STAGE_CANARY_VALIDATED, production_eligible=False)
        with self.assertRaises(ActivationError):
            self.svc.activate(
                cand,
                actor_ref="ops:alice",
                expected_policy_version=ROUTING_POLICY_VERSION,
                now=T0,
            )

    def test_rollback_and_visible_policy(self):
        self.svc.activate(
            _eligible(),
            actor_ref="ops:alice",
            expected_policy_version=ROUTING_POLICY_VERSION,
            now=T0,
        )
        self.svc.rollback(actor_ref="ops:bob", now=T0 + timedelta(seconds=1))
        self.assertIsNone(self.svc.get_active())
        self.assertIsNone(self.svc.active_policy_version)
        types = [e.event_type for e in self.svc.events()]
        self.assertTrue(any("activat" in t for t in types))
        self.assertTrue(any("rollback" in t or "rolled" in t for t in types))

    def test_no_automatic_activation_on_governor(self):
        self.assertFalse(hasattr(PromotionGovernor, "apply_production"))
        self.assertFalse(hasattr(PromotionGovernor, "activate_production"))


if __name__ == "__main__":
    unittest.main()
