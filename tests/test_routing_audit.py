"""P0.4 RoutingDecision auditability — offline only."""

from __future__ import annotations

from decimal import Decimal
import unittest
from unittest.mock import MagicMock

from agents.model_profile import build_model_profile
from agents.model_router import (
    REASON_AUTO_BUDGET_MATCH,
    REASON_AUTO_CAPABILITY_MATCH,
    REASON_EXPLICIT_PROVIDER,
    BudgetRoutingDeniedError,
    ModelRouter,
    NoCapableProviderError,
    ProviderCapabilityMismatchError,
    RoutingDecision,
)
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.routing_audit import (
    REJECT_BUDGET_DENIED,
    REJECT_CAPABILITY_MISMATCH,
    REJECT_UNKNOWN_COST_DENIED,
    RoutingCandidateAudit,
    RoutingFactorSnapshot,
    routing_decision_audit_metadata,
)
from agents.routing_requirements import CAPABILITY_CODING, TaskRequirements
from finops.budget_guard import BudgetGuard
from finops.budget_models import BudgetPolicy, SCOPE_GLOBAL
from finops.models import UNKNOWN_COST_DENY, BudgetLimits, PriceQuote
from finops.service import FinOpsService


def _profile(provider_id, model, *, coding=True, categories="general,technical", quality="standard"):
    return build_model_profile(
        provider_id,
        model,
        task_categories_raw=categories,
        coding_raw="true" if coding else "false",
        quality_raw=quality,
        cost_raw="standard",
    )


def _registry(*, openai_coding=True, anthropic_coding=True, policy="quality"):
    profiles = {
        "openai": _profile("openai", "premium", coding=openai_coding, quality="premium"),
        "anthropic": _profile(
            "anthropic", "cheap", coding=anthropic_coding, quality="standard"
        ),
    }
    records = {
        "openai": ProviderRecord("openai", "premium", True),
        "anthropic": ProviderRecord("anthropic", "cheap", True),
    }
    for pid in ("gemini", "grok", "deepseek", "moonshot", "mistral"):
        records[pid] = ProviderRecord(pid, f"{pid}-m", False)
        profiles[pid] = _profile(pid, f"{pid}-m", categories="general")
    return ProviderRegistry(
        records,
        profiles=profiles,
        auto_provider_order=("openai", "anthropic"),
        auto_routing_policy=policy,
        auto_capability_fallback="error",
    )


class RoutingAuditTests(unittest.TestCase):
    def test_explicit_contains_audit(self):
        decision = ModelRouter(_registry()).decide("openai", "technical", category="technical")
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)
        self.assertTrue(decision.candidates_considered)
        self.assertEqual(decision.candidates_considered[0].provider_id, "openai")
        self.assertTrue(decision.candidates_considered[0].eligible)
        self.assertEqual(decision.factor_snapshot.selected_provider, "openai")
        self.assertEqual(decision.factor_snapshot.selected_model, "premium")

    def test_auto_records_considered(self):
        decision = ModelRouter(_registry()).decide(
            "auto", "technical", category="technical"
        )
        self.assertEqual(decision.provider_ids, ("openai",))
        ids = {c.provider_id for c in decision.candidates_considered}
        self.assertIn("openai", ids)
        self.assertIn("anthropic", ids)
        eligible = [c for c in decision.candidates_considered if c.eligible]
        self.assertTrue(any(c.provider_id == "openai" for c in eligible))

    def test_capability_rejected_reason(self):
        req = TaskRequirements(required_capabilities=(CAPABILITY_CODING,))
        with self.assertRaises(NoCapableProviderError) as ctx:
            ModelRouter(_registry(openai_coding=False, anthropic_coding=False)).decide(
                "auto",
                "technical",
                category="technical",
                requirements=req,
            )
        rejected = ctx.exception.rejected_candidates
        self.assertTrue(rejected)
        self.assertTrue(
            all(r.rejection_reason == REJECT_CAPABILITY_MISMATCH for r in rejected)
        )

    def test_budget_rejected_reason(self):
        finops = FinOpsService(
            prices={
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("10000"), Decimal("10000"), "USD", True
                ),
            },
            limits=BudgetLimits(None, None, None, "allow"),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("1")),),
            required=True,
        )
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        with self.assertRaises(BudgetRoutingDeniedError) as ctx:
            ModelRouter(_registry()).decide(
                "auto",
                "technical",
                category="technical",
                budget_constraints=constraints,
            )
        self.assertTrue(
            any(
                r.rejection_reason == REJECT_BUDGET_DENIED
                for r in ctx.exception.rejected_candidates
            )
        )

    def test_selected_in_factor_snapshot(self):
        decision = ModelRouter(_registry()).decide(
            "auto", "technical", category="technical"
        )
        self.assertEqual(decision.factor_snapshot.selected_provider, "openai")
        self.assertEqual(decision.factor_snapshot.quality_class, "premium")
        self.assertEqual(decision.reason, REASON_AUTO_CAPABILITY_MATCH)

    def test_empty_requirements_same_selection(self):
        reg = _registry()
        a = ModelRouter(reg).decide("auto", "technical", category="technical")
        b = ModelRouter(reg).decide(
            "auto",
            "technical",
            category="technical",
            requirements=TaskRequirements(),
        )
        self.assertEqual(a.provider_ids, b.provider_ids)

    def test_no_budget_same_selection(self):
        reg = _registry()
        a = ModelRouter(reg).decide("auto", "technical", category="technical")
        b = ModelRouter(reg).decide(
            "auto", "technical", category="technical", budget_constraints=None
        )
        self.assertEqual(a.provider_ids, b.provider_ids)

    def test_audit_does_not_alter_ranking(self):
        reg = _registry(policy="quality")
        decision = ModelRouter(reg).decide("auto", "technical", category="technical")
        self.assertEqual(decision.provider_ids, ("openai",))

        finops = FinOpsService(
            prices={
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            },
            limits=BudgetLimits(None, None, None, "allow"),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("1")),),
            required=True,
        )
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        budgeted = ModelRouter(reg).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        self.assertEqual(budgeted.provider_ids, ("anthropic",))
        self.assertEqual(budgeted.reason, REASON_AUTO_BUDGET_MATCH)

    def test_routing_decision_immutable(self):
        decision = ModelRouter(_registry()).decide("openai", "critic")
        with self.assertRaises(Exception):
            decision.reason = "mutated"  # type: ignore[misc]
        with self.assertRaises(Exception):
            decision.provider_ids = ("anthropic",)  # type: ignore[misc]

    def test_candidate_audit_immutable(self):
        decision = ModelRouter(_registry()).decide("openai", "critic")
        row = decision.candidates_considered[0]
        with self.assertRaises(Exception):
            row.provider_id = "x"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            decision.candidates_considered.append(  # type: ignore[attr-defined]
                RoutingCandidateAudit("x")
            )

    def test_factor_snapshot_immutable_defensive(self):
        extra = {"role_id": "technical", "secret_probe": "should-copy"}
        snap = RoutingFactorSnapshot(category="technical", extra=extra)
        extra["role_id"] = "mutated"
        self.assertEqual(snap.extra["role_id"], "technical")
        with self.assertRaises(TypeError):
            snap.extra["role_id"] = "again"  # type: ignore[index]
        decision = ModelRouter(_registry()).decide(
            "auto", "technical", category="technical"
        )
        with self.assertRaises(Exception):
            decision.factor_snapshot.category = "x"  # type: ignore[misc]

    def test_observability_receives_audit_metadata(self):
        router = ModelRouter(_registry())
        obs = MagicMock()
        obs.create_context.return_value = object()
        emitted = {}

        def capture(event_type, **kwargs):
            emitted["event_type"] = event_type
            emitted["metadata"] = kwargs.get("metadata")

        obs.emit.side_effect = capture
        router.observability = obs
        # Patch safe_emit path by making emit work through helper
        decision = router.decide("auto", "technical", category="technical")
        self.assertEqual(emitted.get("event_type"), "provider.selected")
        meta = emitted["metadata"]
        self.assertEqual(meta["route_reason"], decision.reason)
        self.assertIn("candidates_considered", meta)
        self.assertIn("rejection_reason_codes", meta)
        self.assertIn("factor_snapshot", meta)
        self.assertEqual(meta["routing_policy_version"], decision.routing_policy_version)

    def test_serialized_audit_has_no_secrets(self):
        decision = ModelRouter(_registry()).decide(
            "auto", "technical", category="technical"
        )
        meta = routing_decision_audit_metadata(
            reason=decision.reason,
            provider_ids=decision.provider_ids,
            routing_policy_version=decision.routing_policy_version,
            candidates_considered=decision.candidates_considered,
            rejected_candidates=decision.rejected_candidates,
            factor_snapshot=decision.factor_snapshot,
        )
        blob = str(meta).lower()
        for needle in ("api_key", "sk-", "password", "authorization", "prompt"):
            self.assertNotIn(needle, blob)

    def test_explicit_capability_mismatch_audit(self):
        req = TaskRequirements(required_capabilities=(CAPABILITY_CODING,))
        with self.assertRaises(ProviderCapabilityMismatchError) as ctx:
            ModelRouter(_registry(openai_coding=False)).decide(
                "openai",
                "technical",
                category="technical",
                requirements=req,
            )
        self.assertEqual(
            ctx.exception.rejected_candidates[0].rejection_reason,
            REJECT_CAPABILITY_MISMATCH,
        )

    def test_unknown_cost_denied_reason(self):
        finops = FinOpsService(
            prices={
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            },
            limits=BudgetLimits(None, None, None, UNKNOWN_COST_DENY),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("20")),),
            required=True,
        )
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        decision = ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        rejected = {
            c.provider_id: c.rejection_reason for c in decision.rejected_candidates
        }
        self.assertEqual(rejected.get("openai"), REJECT_UNKNOWN_COST_DENIED)


if __name__ == "__main__":
    unittest.main()
