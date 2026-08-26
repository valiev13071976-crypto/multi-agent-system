"""Procurement workflow stages mapped onto existing WorkflowEngine checkpoints."""

from __future__ import annotations

from procurement.models import (
    PROCUREMENT_WORKFLOW_VERSION,
    STATUS_APPROVED,
    STATUS_COMPLETED,
    STATUS_EVALUATING,
    STATUS_FAILED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_OFFERS_READY,
    STATUS_RECOMMENDATION_READY,
    STATUS_REJECTED,
    STATUS_REQUIREMENTS_READY,
    STATUS_RESEARCHING,
    STATUS_WAITING_APPROVAL,
)

STAGES = (
    "normalize_requirements",
    "retrieve_internal_knowledge",
    "discover_suppliers",
    "collect_offers",
    "normalize_offers",
    "validate_offers",
    "compare",
    "analyze_risks",
    "build_recommendation",
    "judge",
    "approval",
    "prepare_action",
    "complete",
)


class ProcurementWorkflow:
    workflow_version = PROCUREMENT_WORKFLOW_VERSION
    stages = STAGES

    def __init__(self, service):
        self.service = service

    def run(self, request_id: str, *, requesting_scope, seed_offers=(), seed_suppliers=(), now=None):
        """Execute MVP stages synchronously against ProcurementService."""
        svc = self.service
        req = svc.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)

        requirement = svc.normalize_requirements(request_id, requesting_scope=requesting_scope)
        if requirement.incomplete:
            svc.update_status(request_id, STATUS_NEEDS_CLARIFICATION, requesting_scope=requesting_scope)
            return {
                "status": STATUS_NEEDS_CLARIFICATION,
                "requirement": requirement,
                "missing_fields": requirement.missing_fields,
            }

        svc.update_status(request_id, STATUS_REQUIREMENTS_READY, requesting_scope=requesting_scope)
        svc.update_status(request_id, STATUS_RESEARCHING, requesting_scope=requesting_scope)

        knowledge_hits = svc.retrieve_internal_knowledge(
            request_id, requesting_scope=requesting_scope
        )
        suppliers = svc.discover_suppliers(
            request_id,
            requesting_scope=requesting_scope,
            seed_suppliers=seed_suppliers,
        )
        offers = svc.collect_offers(
            request_id,
            requesting_scope=requesting_scope,
            seed_offers=seed_offers,
        )
        offers = svc.normalize_offers(request_id, requesting_scope=requesting_scope, now=now)
        svc.update_status(request_id, STATUS_OFFERS_READY, requesting_scope=requesting_scope)
        svc.update_status(request_id, STATUS_EVALUATING, requesting_scope=requesting_scope)

        validated = svc.validate_offers(request_id, requesting_scope=requesting_scope, now=now)
        comparison = svc.compare_offers(request_id, requesting_scope=requesting_scope, now=now)
        risks = svc.analyze_risks(request_id, requesting_scope=requesting_scope, now=now)
        recommendation = svc.build_recommendation(
            request_id,
            requesting_scope=requesting_scope,
            citations=tuple(h.get("citation_ref") for h in knowledge_hits if h.get("citation_ref")),
            now=now,
        )

        # Lightweight judge integration: reuse validator as evidence gate
        judge_ok = recommendation.status == "recommendation_ready"
        if not judge_ok:
            svc.update_status(request_id, STATUS_FAILED, requesting_scope=requesting_scope)
            return {
                "status": STATUS_FAILED,
                "recommendation": recommendation,
                "comparison": comparison,
                "risks": risks,
                "suppliers": suppliers,
                "offers": validated,
            }

        svc.update_status(request_id, STATUS_RECOMMENDATION_READY, requesting_scope=requesting_scope)
        if recommendation.requires_approval:
            svc.update_status(request_id, STATUS_WAITING_APPROVAL, requesting_scope=requesting_scope)
            approval = svc.request_approval(request_id, requesting_scope=requesting_scope)
            return {
                "status": STATUS_WAITING_APPROVAL,
                "recommendation": recommendation,
                "comparison": comparison,
                "risks": risks,
                "approval": approval,
                "suppliers": suppliers,
                "offers": validated,
            }

        action = svc.prepare_action(request_id, requesting_scope=requesting_scope)
        svc.update_status(request_id, STATUS_COMPLETED, requesting_scope=requesting_scope)
        return {
            "status": STATUS_COMPLETED,
            "recommendation": recommendation,
            "action": action,
            "comparison": comparison,
            "risks": risks,
            "suppliers": suppliers,
            "offers": validated,
        }

    def resolve_approval(
        self,
        request_id: str,
        *,
        requesting_scope,
        approved: bool,
        approved_by: str = "operator",
    ):
        if approved:
            self.service.approve(request_id, requesting_scope=requesting_scope, approved_by=approved_by)
            action = self.service.prepare_action(request_id, requesting_scope=requesting_scope)
            self.service.update_status(request_id, STATUS_COMPLETED, requesting_scope=requesting_scope)
            self.service.persist_decision_memory(request_id, requesting_scope=requesting_scope)
            return {"status": STATUS_APPROVED, "action": action}
        self.service.reject(request_id, requesting_scope=requesting_scope, rejected_by=approved_by)
        return {"status": STATUS_REJECTED, "action": None}
