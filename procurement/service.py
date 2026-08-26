"""Canonical ProcurementService — business vertical coordinator."""

from __future__ import annotations

import uuid
from datetime import datetime

from autonomy.models import sanitize_metadata
from memory.models import MEMORY_SEMANTIC, SOURCE_OPERATOR, MemoryIngestRequest, utc_now
from procurement.access import OP_APPROVE, OP_READ, OP_WRITE, ProcurementAccessPolicy
from procurement.comparator import SupplierComparator
from procurement.discovery import SupplierDiscoveryService
from procurement.errors import (
    PROCUREMENT_ACTION_DENIED,
    PROCUREMENT_APPROVAL_REQUIRED,
    PROCUREMENT_NO_SUPPLIERS,
    PROCUREMENT_NO_VALID_OFFERS,
    PROCUREMENT_POLICY_DENIED,
    ProcurementError,
)
from procurement.models import (
    ACTION_EXPORT_COMPARISON,
    ACTION_PREPARE_PURCHASE_REQUEST,
    FORBIDDEN_EXECUTION_ACTIONS,
    STATUS_APPROVED,
    STATUS_CREATED,
    STATUS_REJECTED,
    ProcurementProposedAction,
    ProcurementRequest,
    Supplier,
    SupplierOffer,
)
from procurement.normalizer import OfferNormalizer, normalize_requirements
from procurement.policy import ProcurementPolicy
from procurement.recommendation import ProcurementRecommendationService
from procurement.repos import InMemoryOfferRepository, InMemoryRequestStore, InMemorySupplierRepository
from procurement.risk import ProcurementRiskAnalyzer
from procurement.validator import ProcurementValidator


class ProcurementService:
    def __init__(
        self,
        *,
        policy: ProcurementPolicy | None = None,
        access: ProcurementAccessPolicy | None = None,
        validator: ProcurementValidator | None = None,
        request_store: InMemoryRequestStore | None = None,
        supplier_repo: InMemorySupplierRepository | None = None,
        offer_repo: InMemoryOfferRepository | None = None,
        discovery: SupplierDiscoveryService | None = None,
        normalizer: OfferNormalizer | None = None,
        comparator: SupplierComparator | None = None,
        risk_analyzer: ProcurementRiskAnalyzer | None = None,
        recommendation_service: ProcurementRecommendationService | None = None,
        knowledge_service=None,
        document_service=None,
        memory_service=None,
        workflow_engine=None,
        autonomy_gate=None,
        hitl_service=None,
        observability=None,
        enabled: bool = True,
    ):
        self.policy = policy or ProcurementPolicy()
        self.access = access or ProcurementAccessPolicy()
        self.validator = validator or ProcurementValidator()
        self.request_store = request_store or InMemoryRequestStore()
        self.supplier_repo = supplier_repo or InMemorySupplierRepository()
        self.offer_repo = offer_repo or InMemoryOfferRepository()
        self.normalizer = normalizer or OfferNormalizer()
        self.comparator = comparator or SupplierComparator()
        self.risk_analyzer = risk_analyzer or ProcurementRiskAnalyzer()
        self.recommendation_service = recommendation_service or ProcurementRecommendationService(
            comparator=self.comparator,
            risk_analyzer=self.risk_analyzer,
            validator=self.validator,
            policy=self.policy,
        )
        self.discovery = discovery or SupplierDiscoveryService(
            supplier_repo=self.supplier_repo,
            knowledge_service=knowledge_service,
            document_service=document_service,
        )
        self.knowledge_service = knowledge_service
        self.document_service = document_service
        self.memory_service = memory_service
        self.workflow_engine = workflow_engine
        self.autonomy_gate = autonomy_gate
        self.hitl_service = hitl_service
        self.observability = observability
        self.enabled = bool(enabled)
        self.blocked_reason: str | None = None
        self._approvals: dict[str, dict] = {}
        self._actions: dict[str, ProcurementProposedAction] = {}
        self._last_comparison: dict[str, tuple] = {}
        self._last_risks: dict[str, tuple] = {}
        self._knowledge_citations: dict[str, tuple] = {}

    def create_request(self, request: ProcurementRequest, *, requesting_scope=None) -> ProcurementRequest:
        if not self.enabled or self.blocked_reason:
            raise ProcurementError(self.blocked_reason or "procurement_disabled")
        scope = requesting_scope or request.scope
        self.access.require(requesting=scope, target=request.scope, operation=OP_WRITE)
        self.validator.validate_request(request)
        row = self.request_store.save_request(request)
        self._emit("procurement.request_created", status=STATUS_CREATED)
        self._metric("procurement_requests_total", status=STATUS_CREATED)
        return row

    def get_request(self, request_id: str, *, requesting_scope) -> ProcurementRequest | None:
        return self.request_store.get_request(request_id, requesting_scope=requesting_scope)

    def update_status(self, request_id: str, status: str, *, requesting_scope) -> ProcurementRequest:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)
        self.access.require(requesting=requesting_scope, target=req.scope, operation=OP_WRITE)
        updated = ProcurementRequest(
            request_id=req.request_id,
            scope=req.scope,
            requested_by=req.requested_by,
            item_name=req.item_name,
            quantity=req.quantity,
            unit=req.unit,
            specifications=dict(req.specifications),
            description=req.description,
            target_budget=req.target_budget,
            currency=req.currency,
            required_by=req.required_by,
            delivery_location=req.delivery_location,
            preferred_suppliers=req.preferred_suppliers,
            excluded_suppliers=req.excluded_suppliers,
            constraints=dict(req.constraints),
            metadata_safe=dict(req.metadata_safe),
            status=status,
            created_at=req.created_at,
            updated_at=utc_now(),
            version=req.version + 1,
        )
        return self.request_store.save_request(updated)

    def normalize_requirements(self, request_id: str, *, requesting_scope):
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)
        self.access.require(requesting=requesting_scope, target=req.scope, operation=OP_WRITE)
        requirement = normalize_requirements(req)
        self.request_store.save_requirement(request_id, requirement)
        self._emit(
            "procurement.requirements_normalized",
            status="incomplete" if requirement.incomplete else "ready",
        )
        return requirement

    def retrieve_internal_knowledge(self, request_id: str, *, requesting_scope) -> list[dict]:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)
        self.access.require(requesting=requesting_scope, target=req.scope, operation=OP_READ)
        self._emit("procurement.research_started", status="started")
        hits = []
        if self.knowledge_service is None:
            self._knowledge_citations[request_id] = ()
            return hits
        try:
            from knowledge.models import KnowledgeQuery

            rows = self.knowledge_service.retrieve(
                KnowledgeQuery(query_text=req.item_name, scope=req.scope, limit=10),
                requesting_scope=requesting_scope,
            )
            for row in rows:
                hits.append(
                    {
                        "citation_ref": row.citation_ref,
                        "trust_level": row.trust_level,
                        "source_type": row.source_type,
                        "content_preview": str(row.content)[:120],
                    }
                )
        except Exception:
            hits = []
        self._knowledge_citations[request_id] = tuple(h["citation_ref"] for h in hits)
        return hits

    def discover_suppliers(
        self,
        request_id: str,
        *,
        requesting_scope,
        seed_suppliers: tuple[Supplier, ...] = (),
    ) -> tuple[Supplier, ...]:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        requirement = self.request_store.get_requirement(request_id)
        if req is None or requirement is None:
            raise KeyError(request_id)
        self.access.require(requesting=requesting_scope, target=req.scope, operation=OP_WRITE)
        suppliers = self.discovery.discover(
            scope=req.scope,
            requirement=requirement,
            seed_suppliers=seed_suppliers,
            excluded_supplier_ids=req.excluded_suppliers,
        )
        self._emit("procurement.suppliers_found", status="ok", metadata={"count": len(suppliers)})
        self._metric("procurement_suppliers_considered_total", status="ok")
        if not suppliers:
            raise ProcurementError(PROCUREMENT_NO_SUPPLIERS)
        return suppliers

    def collect_offers(
        self,
        request_id: str,
        *,
        requesting_scope,
        seed_offers: tuple[SupplierOffer, ...] = (),
    ) -> tuple[SupplierOffer, ...]:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)
        self.access.require(requesting=requesting_scope, target=req.scope, operation=OP_WRITE)
        for offer in seed_offers:
            if offer.scope.key() != req.scope.key() or offer.request_id != request_id:
                continue
            self.offer_repo.upsert(offer)
            self._metric("procurement_offers_total", status=offer.status)
        return self.offer_repo.list_for_request(request_id, scope=req.scope)

    def normalize_offers(self, request_id: str, *, requesting_scope, now=None) -> tuple[SupplierOffer, ...]:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)
        out = []
        for offer in self.offer_repo.list_for_request(request_id, scope=req.scope):
            normalized = self.normalizer.normalize(offer, now=now)
            self.offer_repo.upsert(normalized)
            out.append(normalized)
        self._emit("procurement.offers_normalized", status="ok", metadata={"count": len(out)})
        return tuple(out)

    def validate_offers(self, request_id: str, *, requesting_scope, now=None) -> tuple[SupplierOffer, ...]:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)
        stamp = now or utc_now()
        valid = []
        for offer in self.offer_repo.list_for_request(request_id, scope=req.scope):
            issues = self.validator.validate_offer(
                offer, now=stamp, require_provenance=self.policy.require_price_provenance
            )
            if issues:
                self._emit("procurement.offer_rejected", status="rejected", metadata={"reason": issues[0]})
                self._metric("procurement_offer_rejected_total", status="rejected")
                rejected = SupplierOffer(
                    offer_id=offer.offer_id,
                    request_id=offer.request_id,
                    supplier_id=offer.supplier_id,
                    scope=offer.scope,
                    source_type=offer.source_type,
                    source_ref=offer.source_ref,
                    currency=offer.currency,
                    unit_price=offer.unit_price,
                    quantity=offer.quantity,
                    provenance=offer.provenance,
                    subtotal=offer.subtotal,
                    shipping_cost=offer.shipping_cost,
                    tax=offer.tax,
                    total_cost=offer.total_cost,
                    lead_time_days=offer.lead_time_days,
                    minimum_order_quantity=offer.minimum_order_quantity,
                    payment_terms=offer.payment_terms,
                    delivery_terms=offer.delivery_terms,
                    valid_until=offer.valid_until,
                    availability=offer.availability,
                    warranty=offer.warranty,
                    specifications=dict(offer.specifications),
                    compliance=dict(offer.compliance),
                    confidence=offer.confidence,
                    status="rejected" if "procurement_provenance_missing" in issues else (
                        "expired" if "procurement_offer_expired" in issues else offer.status
                    ),
                    metadata_safe={**dict(offer.metadata_safe), "validation_issues": issues},
                    created_at=offer.created_at,
                    updated_at=stamp,
                )
                self.offer_repo.upsert(rejected)
                continue
            validated = SupplierOffer(
                offer_id=offer.offer_id,
                request_id=offer.request_id,
                supplier_id=offer.supplier_id,
                scope=offer.scope,
                source_type=offer.source_type,
                source_ref=offer.source_ref,
                currency=offer.currency,
                unit_price=offer.unit_price,
                quantity=offer.quantity,
                provenance=offer.provenance,
                subtotal=offer.subtotal,
                shipping_cost=offer.shipping_cost,
                tax=offer.tax,
                total_cost=offer.total_cost,
                lead_time_days=offer.lead_time_days,
                minimum_order_quantity=offer.minimum_order_quantity,
                payment_terms=offer.payment_terms,
                delivery_terms=offer.delivery_terms,
                valid_until=offer.valid_until,
                availability=offer.availability,
                warranty=offer.warranty,
                specifications=dict(offer.specifications),
                compliance=dict(offer.compliance),
                confidence=offer.confidence,
                status="validated",
                metadata_safe=dict(offer.metadata_safe),
                created_at=offer.created_at,
                updated_at=stamp,
            )
            self.offer_repo.upsert(validated)
            valid.append(validated)
        return tuple(valid)

    def compare_offers(self, request_id: str, *, requesting_scope, now=None):
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        requirement = self.request_store.get_requirement(request_id)
        if req is None or requirement is None:
            raise KeyError(request_id)
        offers = self.offer_repo.list_for_request(request_id, scope=req.scope)
        suppliers = {s.supplier_id: s for s in self.supplier_repo.list_for_scope(req.scope)}
        comparison = self.comparator.compare(
            offers=offers, suppliers=suppliers, requirement=requirement, now=now
        )
        self._last_comparison[request_id] = comparison
        self._emit("procurement.comparison_completed", status="ok", metadata={"count": len(comparison)})
        return comparison

    def analyze_risks(self, request_id: str, *, requesting_scope, now=None):
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        requirement = self.request_store.get_requirement(request_id)
        if req is None or requirement is None:
            raise KeyError(request_id)
        offers = self.offer_repo.list_for_request(request_id, scope=req.scope)
        suppliers = {s.supplier_id: s for s in self.supplier_repo.list_for_scope(req.scope)}
        comparison = self._last_comparison.get(request_id) or ()
        currencies = {o.currency for o in offers}
        risks = self.risk_analyzer.analyze(
            offers=offers,
            suppliers=suppliers,
            requirement=requirement,
            comparison=comparison,
            single_source=len([o for o in offers if o.status in {"validated", "normalized"}]) == 1,
            currency_conversion_required=len(currencies) > 1,
            now=now,
        )
        self._last_risks[request_id] = risks
        for risk in risks:
            if risk.level in {"high", "critical"}:
                self._emit(
                    "procurement.risk_detected",
                    status=risk.level,
                    metadata={"code": risk.code, "category": risk.category},
                )
        return risks

    def build_recommendation(
        self,
        request_id: str,
        *,
        requesting_scope,
        citations: tuple[str, ...] = (),
        now=None,
    ):
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        requirement = self.request_store.get_requirement(request_id)
        if req is None or requirement is None:
            raise KeyError(request_id)
        offers = self.offer_repo.list_for_request(request_id, scope=req.scope)
        if not offers:
            raise ProcurementError(PROCUREMENT_NO_VALID_OFFERS)
        suppliers = {s.supplier_id: s for s in self.supplier_repo.list_for_scope(req.scope)}
        cites = citations or self._knowledge_citations.get(request_id, ())
        rec = self.recommendation_service.build(
            request=req,
            requirement=requirement,
            offers=offers,
            suppliers=suppliers,
            citations=cites,
            now=now,
        )
        self.request_store.save_recommendation(rec)
        self._emit(
            "procurement.recommendation_created",
            status=rec.status,
            metadata={
                "single_source": rec.single_source_procurement,
                "currency_conversion_required": rec.currency_conversion_required,
            },
        )
        self._metric("procurement_recommendations_total", status=rec.status)
        return rec

    def request_approval(self, request_id: str, *, requesting_scope) -> dict:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        rec = self.request_store.get_recommendation_for_request(
            request_id, requesting_scope=requesting_scope
        )
        if req is None or rec is None:
            raise KeyError(request_id)
        self.access.require(requesting=requesting_scope, target=req.scope, operation=OP_APPROVE)
        offer = None
        if rec.recommended_offer_id:
            offer = self.offer_repo.get(rec.recommended_offer_id, requesting_scope=requesting_scope)
        supplier = None
        if rec.recommended_supplier_id:
            supplier = self.supplier_repo.get(
                rec.recommended_supplier_id, requesting_scope=requesting_scope
            )
        payload = {
            "approval_id": str(uuid.uuid4()),
            "request_id": request_id,
            "item": req.item_name,
            "quantity": str(req.quantity) if req.quantity is not None else None,
            "unit": req.unit,
            "supplier": supplier.name if supplier else None,
            "supplier_id": rec.recommended_supplier_id,
            "offer_id": rec.recommended_offer_id,
            "price": str(offer.unit_price.amount) if offer and offer.unit_price else None,
            "currency": offer.currency if offer else req.currency,
            "total": str(offer.total_cost.amount) if offer and offer.total_cost else None,
            "delivery": offer.delivery_terms if offer else None,
            "key_risks": [r.code for r in rec.risks if r.level in {"high", "critical"}],
            "source_citations": list(rec.citations),
            "intended_action": ACTION_PREPARE_PURCHASE_REQUEST,
            "status": "pending",
        }
        self._approvals[request_id] = payload
        self._emit("procurement.approval_requested", status="pending")
        self._metric("procurement_approval_required_total", status="pending")
        return payload

    def approve(self, request_id: str, *, requesting_scope, approved_by: str = "operator") -> dict:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)
        approval = self._approvals.get(request_id)
        if not approval:
            raise ProcurementError(PROCUREMENT_APPROVAL_REQUIRED)
        approval = {**approval, "status": "approved", "approved_by": approved_by}
        self._approvals[request_id] = approval
        self.update_status(request_id, STATUS_APPROVED, requesting_scope=requesting_scope)
        self._emit("procurement.approved", status="approved")
        return approval

    def reject(self, request_id: str, *, requesting_scope, rejected_by: str = "operator") -> dict:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)
        approval = self._approvals.get(request_id) or {"approval_id": str(uuid.uuid4())}
        approval = {**approval, "status": "rejected", "rejected_by": rejected_by}
        self._approvals[request_id] = approval
        self.update_status(request_id, STATUS_REJECTED, requesting_scope=requesting_scope)
        self._emit("procurement.rejected", status="rejected")
        return approval

    def prepare_action(
        self,
        request_id: str,
        *,
        requesting_scope,
        action_type: str = ACTION_PREPARE_PURCHASE_REQUEST,
    ) -> ProcurementProposedAction:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        rec = self.request_store.get_recommendation_for_request(
            request_id, requesting_scope=requesting_scope
        )
        if req is None or rec is None:
            raise KeyError(request_id)
        approval = self._approvals.get(request_id)
        if self.policy.approval_required and (not approval or approval.get("status") != "approved"):
            if req.status != STATUS_APPROVED:
                raise ProcurementError(PROCUREMENT_APPROVAL_REQUIRED)
        if action_type in FORBIDDEN_EXECUTION_ACTIONS:
            raise ProcurementError(PROCUREMENT_ACTION_DENIED)
        if action_type not in {
            ACTION_PREPARE_PURCHASE_REQUEST,
            ACTION_EXPORT_COMPARISON,
            "prepare_rfq",
            "prepare_supplier_contact",
        }:
            raise ProcurementError(PROCUREMENT_ACTION_DENIED)
        action = ProcurementProposedAction(
            action_id=str(uuid.uuid4()),
            request_id=request_id,
            action_type=action_type,
            payload_safe={
                "item": req.item_name,
                "offer_id": rec.recommended_offer_id,
                "supplier_id": rec.recommended_supplier_id,
                "draft_only": True,
                "execution_disabled": True,
            },
            requires_approval=False,
            status="prepared",
        )
        self._actions[request_id] = action
        return action

    def execute_financial_action(self, action_type: str, **_kwargs) -> None:
        """P16 hard deny — place_order / pay_supplier never execute."""
        self._metric("procurement_failures_total", status="denied")
        raise ProcurementError(PROCUREMENT_ACTION_DENIED, details={"action_type": action_type})

    def find_suppliers(self, *, scope, categories: tuple[str, ...] = ()) -> tuple[Supplier, ...]:
        return self.supplier_repo.find(scope=scope, categories=categories, exclude_restricted=False)

    def find_offers(self, request_id: str, *, scope) -> tuple[SupplierOffer, ...]:
        return self.offer_repo.list_for_request(request_id, scope=scope)

    def persist_decision_memory(self, request_id: str, *, requesting_scope) -> None:
        if self.memory_service is None:
            return
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        rec = self.request_store.get_recommendation_for_request(
            request_id, requesting_scope=requesting_scope
        )
        requirement = self.request_store.get_requirement(request_id)
        if req is None or rec is None:
            return
        content = (
            f"Procurement decision for {req.item_name}: "
            f"offer={rec.recommended_offer_id} supplier={rec.recommended_supplier_id}. "
            f"Rationale: {rec.reasoning_summary}"
        )
        try:
            self.memory_service.ingest(
                MemoryIngestRequest(
                    scope=req.scope,
                    memory_type=MEMORY_SEMANTIC,
                    content=content,
                    source_type=SOURCE_OPERATOR,
                    source_id="procurement_service",
                    confidence=rec.confidence,
                    tags=("procurement", "decision"),
                    created_by_component="procurement_service",
                    external_reference=request_id,
                    metadata_safe={
                        "request_id": request_id,
                        "recommendation_id": rec.recommendation_id,
                        "citations": list(rec.citations)[:10],
                        "single_source": rec.single_source_procurement,
                    },
                ),
                requesting_scope=requesting_scope,
                validated=True,
                auto=False,
            )
        except Exception:
            pass

    def _emit(self, event_type: str, *, status: str = "", metadata: dict | None = None) -> None:
        obs = self.observability
        if obs is None:
            return
        try:
            safe = sanitize_metadata(
                {
                    k: v
                    for k, v in dict(metadata or {}).items()
                    if k
                    not in {
                        "content",
                        "supplier_name",
                        "scope_id",
                        "request_id",
                        "price",
                        "raw",
                    }
                }
            )
            obs.emit(event_type, component="procurement", status=status, metadata=safe)
        except Exception:
            pass

    def _metric(self, name: str, *, status: str, category: str = "general") -> None:
        obs = self.observability
        if obs is None or not getattr(obs, "metrics", None):
            return
        try:
            obs.metrics.inc(
                name,
                labels={
                    "component": "procurement",
                    "status": status,
                    "case_type": category,
                },
            )
        except Exception:
            pass
