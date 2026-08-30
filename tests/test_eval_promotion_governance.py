"""PATCH-MR-06 — offline evals promotion governance contract."""

from __future__ import annotations

import os
import unittest

from agents.model_profile import ROUTING_POLICY_VERSION
from agents.model_router import ModelRouter
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.routing_runtime_stats import DEFAULT_RUNTIME_TIEBREAK_ENABLED
from evals.promotion import (
    STAGE_CANDIDATE,
    STAGE_CANARY_VALIDATED,
    STAGE_PRODUCTION_ELIGIBLE,
    STAGE_SHADOW_VALIDATED,
    CanaryEvidence,
    CandidatePolicy,
    EVIDENCE_BLOCKED,
    EVIDENCE_FAIL,
    EVIDENCE_PASS,
    METRIC_UNAVAILABLE,
    PromotionGovernanceError,
    PromotionGovernor,
    ShadowEvidence,
    measured_metric,
    unavailable_metric,
)
from evals.release_gate import GATE_BLOCKED, GATE_FAIL, GATE_PASS, ReleaseGateDecision
from evals.versions import CORE_SUITE_VERSION


def _registry():
    records = {
        "openai": ProviderRecord("openai", "m1", True),
        "anthropic": ProviderRecord("anthropic", "m2", True),
    }
    for pid in ("gemini", "grok", "deepseek", "moonshot", "mistral"):
        records[pid] = ProviderRecord(pid, f"{pid}-m", False)
    return ProviderRegistry(records, auto_provider_order=("openai", "anthropic"))


def _candidate(
    gov: PromotionGovernor,
    *,
    proposed: str | None = None,
    base: str | None = None,
    candidate_id: str = "cand-1",
) -> CandidatePolicy:
    policy = proposed if proposed is not None else ROUTING_POLICY_VERSION
    return gov.create_candidate(
        candidate_id=candidate_id,
        candidate_version="1.0.0",
        base_routing_policy_version=base if base is not None else ROUTING_POLICY_VERSION,
        proposed_routing_policy_version=policy,
        eval_suite_id="core",
        eval_suite_version=CORE_SUITE_VERSION,
        eval_run_id="run-abc",
        eval_manifest_hash="manifest-hash-1",
        model_profile_version="profile-1.0.0",
        provider_profile_versions={"openai": "p1", "anthropic": "p1"},
        eval_artifact_ref="artifact:run-abc",
    )


def _shadow(
    candidate: CandidatePolicy,
    *,
    status: str = EVIDENCE_PASS,
    policy: str | None = None,
    evidence_id: str = "shadow-1",
) -> ShadowEvidence:
    return ShadowEvidence(
        evidence_id=evidence_id,
        candidate_id=candidate.candidate_id,
        candidate_version=candidate.candidate_version,
        routing_policy_version=policy or candidate.proposed_routing_policy_version,
        evidence_source="offline_eval_replay",
        overall_status=status,
        quality=measured_metric(1.0),
        latency=unavailable_metric(),
        cost=unavailable_metric(),
        routing_divergence=unavailable_metric(),
    )


def _canary(
    candidate: CandidatePolicy,
    *,
    status: str = EVIDENCE_PASS,
    policy: str | None = None,
    evidence_id: str = "canary-1",
) -> CanaryEvidence:
    return CanaryEvidence(
        evidence_id=evidence_id,
        candidate_id=candidate.candidate_id,
        candidate_version=candidate.candidate_version,
        routing_policy_version=policy or candidate.proposed_routing_policy_version,
        evidence_source="offline_canary_fixture",
        overall_status=status,
        quality=measured_metric(1.0),
        latency=unavailable_metric(),
        cost=unavailable_metric(),
        error_rate=unavailable_metric(),
    )


class PromotionGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.gov = PromotionGovernor()

    def test_case1_candidate_creation_non_mutating(self):
        reg = _registry()
        router = ModelRouter(reg)
        before_order = tuple(reg.auto_provider_order)
        before_policy = ROUTING_POLICY_VERSION
        before_env = dict(os.environ)
        before_tiebreak = DEFAULT_RUNTIME_TIEBREAK_ENABLED

        cand = _candidate(self.gov)
        self.assertEqual(cand.stage, STAGE_CANDIDATE)
        self.assertFalse(cand.production_eligible)
        self.assertFalse(cand.production_active)
        self.assertTrue(cand.content_hash)

        self.assertEqual(tuple(reg.auto_provider_order), before_order)
        self.assertEqual(ROUTING_POLICY_VERSION, before_policy)
        self.assertFalse(DEFAULT_RUNTIME_TIEBREAK_ENABLED)
        self.assertEqual(DEFAULT_RUNTIME_TIEBREAK_ENABLED, before_tiebreak)
        # No production apply env keys introduced.
        self.assertEqual(
            {k: v for k, v in os.environ.items() if k.startswith("ROUTING_")},
            {k: v for k, v in before_env.items() if k.startswith("ROUTING_")},
        )
        # Live decide still works with unchanged policy pin.
        decision = router.decide("auto", "technical", category="technical")
        self.assertEqual(decision.routing_policy_version, ROUTING_POLICY_VERSION)

    def test_case2_candidate_tied_to_base_and_proposed(self):
        cand = _candidate(
            self.gov,
            base="1.0.0",
            proposed="1.0.0",
        )
        self.assertEqual(cand.base_routing_policy_version, "1.0.0")
        self.assertEqual(cand.proposed_routing_policy_version, "1.0.0")
        with self.assertRaises(PromotionGovernanceError) as ctx:
            self.gov.create_candidate(
                candidate_id="x",
                candidate_version="1",
                base_routing_policy_version="",
                proposed_routing_policy_version="1.0.0",
                eval_suite_id="core",
                eval_suite_version="1",
                eval_run_id="r",
                eval_manifest_hash="h",
                model_profile_version="p",
            )
        self.assertEqual(ctx.exception.reason_code, "routing_policy_version_required")

    def test_case3_shadow_pass(self):
        cand = _candidate(self.gov)
        advanced = self.gov.apply_shadow(cand, _shadow(cand))
        self.assertEqual(advanced.stage, STAGE_SHADOW_VALIDATED)
        self.assertEqual(advanced.shadow_evidence_id, "shadow-1")
        self.assertFalse(advanced.production_eligible)

    def test_case4_shadow_fail(self):
        cand = _candidate(self.gov)
        with self.assertRaises(PromotionGovernanceError) as ctx:
            self.gov.apply_shadow(cand, _shadow(cand, status=EVIDENCE_FAIL))
        self.assertEqual(ctx.exception.reason_code, "shadow_evidence_not_pass")
        self.assertEqual(cand.stage, STAGE_CANDIDATE)

    def test_case5_canary_cannot_precede_shadow(self):
        cand = _candidate(self.gov)
        with self.assertRaises(PromotionGovernanceError) as ctx:
            self.gov.apply_canary(cand, _canary(cand))
        self.assertEqual(ctx.exception.reason_code, "canary_requires_shadow_validated")

    def test_case6_canary_pass_after_shadow(self):
        base = _candidate(self.gov)
        shadowed = self.gov.apply_shadow(base, _shadow(base))
        canaried = self.gov.apply_canary(shadowed, _canary(shadowed))
        self.assertEqual(canaried.stage, STAGE_CANARY_VALIDATED)
        self.assertEqual(canaried.canary_evidence_id, "canary-1")
        self.assertFalse(canaried.production_eligible)

    def test_case7_canary_fail_not_eligible(self):
        base = _candidate(self.gov)
        shadowed = self.gov.apply_shadow(base, _shadow(base))
        with self.assertRaises(PromotionGovernanceError) as ctx:
            self.gov.apply_canary(shadowed, _canary(shadowed, status=EVIDENCE_BLOCKED))
        self.assertEqual(ctx.exception.reason_code, "canary_evidence_not_pass")
        self.assertFalse(shadowed.production_eligible)

    def test_case8_release_gate_pass_after_required_stages(self):
        base = _candidate(self.gov)
        shadowed = self.gov.apply_shadow(base, _shadow(base))
        canaried = self.gov.apply_canary(shadowed, _canary(shadowed))
        eligible = self.gov.apply_release_gate(
            canaried, ReleaseGateDecision(GATE_PASS)
        )
        self.assertEqual(eligible.stage, STAGE_PRODUCTION_ELIGIBLE)
        self.assertTrue(eligible.production_eligible)
        self.assertFalse(eligible.production_active)
        self.assertEqual(eligible.release_gate_decision, GATE_PASS)
        stages = [t.to_stage for t in eligible.transitions]
        self.assertIn(STAGE_PRODUCTION_ELIGIBLE, stages)

    def test_case9_release_gate_pass_without_required_stages(self):
        cand = _candidate(self.gov)
        with self.assertRaises(PromotionGovernanceError) as ctx:
            self.gov.apply_release_gate(cand, ReleaseGateDecision(GATE_PASS))
        self.assertEqual(ctx.exception.reason_code, "release_gate_requires_canary_validated")
        self.assertFalse(cand.production_eligible)

    def test_case10_release_gate_fail_or_blocked(self):
        base = _candidate(self.gov)
        shadowed = self.gov.apply_shadow(base, _shadow(base))
        canaried = self.gov.apply_canary(shadowed, _canary(shadowed))
        with self.assertRaises(PromotionGovernanceError) as ctx:
            self.gov.apply_release_gate(
                canaried, ReleaseGateDecision(GATE_FAIL, reason_codes=("baseline_regression",))
            )
        self.assertEqual(ctx.exception.reason_code, "release_gate_not_pass")
        with self.assertRaises(PromotionGovernanceError) as ctx2:
            self.gov.apply_release_gate(
                canaried, ReleaseGateDecision(GATE_BLOCKED, reason_codes=("suite_blocked",))
            )
        self.assertEqual(ctx2.exception.reason_code, "release_gate_not_pass")

    def test_case11_production_eligible_is_not_active(self):
        base = _candidate(self.gov)
        shadowed = self.gov.apply_shadow(base, _shadow(base))
        canaried = self.gov.apply_canary(shadowed, _canary(shadowed))
        eligible = self.gov.apply_release_gate(
            canaried, ReleaseGateDecision(GATE_PASS)
        )
        self.assertTrue(eligible.production_eligible)
        self.assertFalse(eligible.production_active)
        with self.assertRaises(PromotionGovernanceError):
            CandidatePolicy(
                candidate_id="x",
                candidate_version="1",
                base_routing_policy_version="1.0.0",
                proposed_routing_policy_version="1.0.0",
                stage=STAGE_PRODUCTION_ELIGIBLE,
                eval_suite_id="core",
                eval_suite_version="1",
                eval_run_id="r",
                eval_manifest_hash="h",
                model_profile_version="p",
                production_eligible=True,
                production_active=True,
            )

    def test_case12_no_automatic_router_mutation(self):
        reg = _registry()
        router = ModelRouter(reg)
        before = (
            ROUTING_POLICY_VERSION,
            tuple(reg.auto_provider_order),
            DEFAULT_RUNTIME_TIEBREAK_ENABLED,
            os.environ.get("ROUTING_RUNTIME_TIEBREAK_ENABLED"),
        )
        base = _candidate(self.gov)
        shadowed = self.gov.apply_shadow(base, _shadow(base))
        canaried = self.gov.apply_canary(shadowed, _canary(shadowed))
        eligible = self.gov.apply_release_gate(
            canaried, ReleaseGateDecision(GATE_PASS)
        )
        self.assertTrue(eligible.production_eligible)
        after = (
            ROUTING_POLICY_VERSION,
            tuple(reg.auto_provider_order),
            DEFAULT_RUNTIME_TIEBREAK_ENABLED,
            os.environ.get("ROUTING_RUNTIME_TIEBREAK_ENABLED"),
        )
        self.assertEqual(before, after)
        self.assertFalse(hasattr(self.gov, "apply_production"))
        decision = router.decide("auto", "technical", category="technical")
        self.assertEqual(decision.routing_policy_version, ROUTING_POLICY_VERSION)

    def test_case13_artifact_reproducibility(self):
        cand = _candidate(self.gov)
        payload = cand.identity_payload()
        self.assertEqual(payload["eval_run_id"], "run-abc")
        self.assertEqual(payload["eval_manifest_hash"], "manifest-hash-1")
        self.assertEqual(payload["eval_suite_version"], CORE_SUITE_VERSION)
        self.assertEqual(payload["model_profile_version"], "profile-1.0.0")
        self.assertEqual(payload["base_routing_policy_version"], ROUTING_POLICY_VERSION)
        self.assertEqual(
            payload["proposed_routing_policy_version"], ROUTING_POLICY_VERSION
        )
        self.assertIn("openai", payload["provider_profile_versions"])
        self.assertEqual(cand.content_hash, CandidatePolicy(**{
            **{k: getattr(cand, k) for k in (
                "candidate_id",
                "candidate_version",
                "base_routing_policy_version",
                "proposed_routing_policy_version",
                "stage",
                "eval_suite_id",
                "eval_suite_version",
                "eval_run_id",
                "eval_manifest_hash",
                "model_profile_version",
            )},
            "provider_profile_versions": dict(cand.provider_profile_versions),
            "eval_artifact_ref": cand.eval_artifact_ref,
            "created_at": cand.created_at,
            "updated_at": cand.updated_at,
            "transitions": cand.transitions,
        }).content_hash)

    def test_case14_missing_metric_honesty(self):
        base = _candidate(self.gov)
        evidence = _shadow(base)
        self.assertEqual(evidence.latency.status, METRIC_UNAVAILABLE)
        self.assertIsNone(evidence.latency.value)
        self.assertEqual(evidence.cost.status, METRIC_UNAVAILABLE)
        self.assertIsNone(evidence.cost.value)
        # Measured quality does not invent latency/cost PASS values.
        self.assertEqual(evidence.quality.status, "measured")
        as_dict = evidence.as_dict()
        self.assertEqual(as_dict["latency"]["status"], METRIC_UNAVAILABLE)
        self.assertIsNone(as_dict["latency"]["value"])

    def test_case15_invalid_transition_fail_closed(self):
        cand = _candidate(self.gov)
        # Skip shadow → attempt release
        with self.assertRaises(PromotionGovernanceError) as ctx:
            self.gov.apply_release_gate(cand, ReleaseGateDecision(GATE_PASS))
        self.assertEqual(ctx.exception.reason_code, "release_gate_requires_canary_validated")
        # Replay shadow after already shadowed is invalid (stage moved)
        shadowed = self.gov.apply_shadow(cand, _shadow(cand))
        with self.assertRaises(PromotionGovernanceError) as ctx2:
            self.gov.apply_shadow(shadowed, _shadow(shadowed, evidence_id="shadow-2"))
        self.assertEqual(ctx2.exception.reason_code, "shadow_requires_candidate_stage")

    def test_case16_routing_policy_mismatch(self):
        cand = _candidate(self.gov, proposed="1.0.0")
        with self.assertRaises(PromotionGovernanceError) as ctx:
            self.gov.apply_shadow(cand, _shadow(cand, policy="9.9.9"))
        self.assertEqual(ctx.exception.reason_code, "routing_policy_version_mismatch")
        shadowed_ok = self.gov.apply_shadow(cand, _shadow(cand, policy="1.0.0"))
        with self.assertRaises(PromotionGovernanceError) as ctx2:
            self.gov.apply_canary(shadowed_ok, _canary(shadowed_ok, policy="2.0.0"))
        self.assertEqual(ctx2.exception.reason_code, "routing_policy_version_mismatch")
        canaried = self.gov.apply_canary(shadowed_ok, _canary(shadowed_ok, policy="1.0.0"))
        with self.assertRaises(PromotionGovernanceError) as ctx3:
            self.gov.apply_release_gate(
                canaried,
                ReleaseGateDecision(GATE_PASS),
                expected_routing_policy_version="3.0.0",
            )
        self.assertEqual(ctx3.exception.reason_code, "routing_policy_version_mismatch")
        self.assertFalse(canaried.production_eligible)


if __name__ == "__main__":
    unittest.main()
