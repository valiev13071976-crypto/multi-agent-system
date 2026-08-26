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
        tool_gateway=None,
        external_research_policy=None,
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
        from procurement.adapters.policy import ProcurementExternalResearchPolicy

        self.external_research_policy = external_research_policy or ProcurementExternalResearchPolicy()
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
        self.tool_gateway = tool_gateway
        self.enabled = bool(enabled)
        self.blocked_reason: str | None = None
        self._approvals: dict[str, dict] = {}
        self._actions: dict[str, ProcurementProposedAction] = {}
        self._last_comparison: dict[str, tuple] = {}
        self._last_risks: dict[str, tuple] = {}
        self._knowledge_citations: dict[str, tuple] = {}
        self._validated_catalog_refs: set[str] = set()
        self._external_query_count = 0

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
        force_external: bool = False,
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
        # External fallback only when policy enables and internal insufficient
        if self.external_research_policy.should_call_external(
            internal_supplier_count=len(suppliers), force=force_external
        ):
            try:
                external = self.search_external_suppliers(
                    product_name=requirement.normalized_item,
                    category=requirement.category,
                    limit=self.external_research_policy.max_results_per_query,
                    requesting_scope=requesting_scope,
                    scope=req.scope,
                )
                merged = {s.supplier_id: s for s in suppliers}
                for s in external:
                    merged.setdefault(s.supplier_id, s)
                    self.supplier_repo.upsert(s)
                    self._validated_catalog_refs.add(s.source_ref)
                    if s.website_ref:
                        self._validated_catalog_refs.add(s.website_ref)
                suppliers = tuple(sorted(merged.values(), key=lambda x: x.supplier_id))
            except ProcurementError as exc:
                if exc.reason == "procurement_external_search_disabled":
                    pass
                else:
                    self._emit(
                        "procurement.external_search_failed",
                        status="failed",
                        metadata={"reason": exc.reason},
                    )
        self._emit("procurement.suppliers_found", status="ok", metadata={"count": len(suppliers)})
        self._metric("procurement_suppliers_considered_total", status="ok")
        if not suppliers:
            raise ProcurementError(PROCUREMENT_NO_SUPPLIERS)
        return suppliers

    def search_external_suppliers(
        self,
        *,
        product_name: str,
        category: str = "general",
        country: str | None = None,
        required_specs: dict | None = None,
        limit: int | None = None,
        requesting_scope=None,
        scope=None,
    ) -> tuple[Supplier, ...]:
        if not self.external_research_policy.enabled:
            raise ProcurementError("procurement_external_search_disabled")
        if self._external_query_count >= self.external_research_policy.max_queries:
            raise ProcurementError("procurement_external_query_invalid", details={"reason": "max_queries"})
        from procurement.adapters.gateway_bridge import invoke_tool_sync
        from procurement.adapters.models import OP_SEARCH, TOOL_SUPPLIER_SEARCH
        from procurement.models import TRUST_EXTERNAL, TRUST_UNVERIFIED, content_hash_text

        self._emit("procurement.external_search_started", status="started")
        started = utc_now()
        try:
            result = invoke_tool_sync(
                self.tool_gateway,
                tool_id=TOOL_SUPPLIER_SEARCH,
                operation=OP_SEARCH,
                arguments={
                    "product_name": product_name,
                    "category": category,
                    "country": country,
                    "required_specs": required_specs or {},
                    "limit": limit or self.external_research_policy.max_results_per_query,
                },
            )
        except ProcurementError as exc:
            self._emit("procurement.external_search_failed", status="failed")
            self._metric(
                "procurement_external_search_failure_total",
                status="failed",
                source_type="search_provider",
                tool_id=TOOL_SUPPLIER_SEARCH,
            )
            raise
        self._external_query_count += 1
        data = dict(getattr(result, "data", None) or {})
        rows = list(data.get("suppliers") or [])[
            : self.external_research_policy.max_total_results
        ]
        out = []
        target_scope = scope or requesting_scope
        for row in rows:
            name = str(row.get("supplier_name") or "").strip()
            ref = str(row.get("supplier_ref") or "")
            if not name or not ref or not target_scope:
                continue
            sid = f"ext-{content_hash_text(ref)[:12]}"
            trust = row.get("trust_level") or TRUST_UNVERIFIED
            if trust not in {TRUST_EXTERNAL, TRUST_UNVERIFIED}:
                trust = TRUST_UNVERIFIED
            supplier = Supplier(
                supplier_id=sid,
                scope=target_scope,
                name=name,
                source="search_provider",
                source_ref=ref,
                categories=(category,),
                trust_level=trust,
                status="candidate",
                country=row.get("country"),
                website_ref=row.get("website_ref"),
                provenance=dict(row.get("provenance") or {}),
                metadata_safe={"from_external_search": True},
            )
            out.append(supplier)
            self._validated_catalog_refs.add(ref)
        elapsed = max(0, int((utc_now() - started).total_seconds() * 1000))
        self._emit(
            "procurement.external_search_completed",
            status="ok",
            metadata={"count": len(out)},
        )
        self._metric(
            "procurement_external_search_total",
            status="ok",
            source_type="search_provider",
            tool_id=TOOL_SUPPLIER_SEARCH,
        )
        self._observe_latency(
            "procurement_external_latency_ms",
            elapsed,
            tool_id=TOOL_SUPPLIER_SEARCH,
            source_type="search_provider",
        )
        return tuple(out)

    def read_supplier_catalog(
        self,
        *,
        supplier_ref: str,
        catalog_ref: str,
        requesting_scope,
        scope,
        limit: int | None = None,
        request_id: str | None = None,
    ):
        from procurement.adapters.gateway_bridge import invoke_tool_sync
        from procurement.adapters.models import OP_CATALOG_READ, TOOL_CATALOG_READ
        from procurement.models import OfferProvenance

        self.access.require(requesting=requesting_scope, target=scope, operation=OP_READ)
        if supplier_ref not in self._validated_catalog_refs and catalog_ref not in self._validated_catalog_refs:
            known = False
            for s in self.supplier_repo.list_for_scope(scope):
                if s.source_ref == supplier_ref or s.supplier_id == supplier_ref:
                    known = True
                    self._validated_catalog_refs.add(s.source_ref)
                    if s.website_ref:
                        self._validated_catalog_refs.add(s.website_ref)
                    break
            if not known:
                raise ProcurementError("procurement_catalog_ref_invalid")
        try:
            reg = getattr(self.tool_gateway, "registry", None)
            if reg is not None:
                registration = reg.get_registration(TOOL_CATALOG_READ)
                adapter = registration.adapter
                if adapter is not None and hasattr(adapter, "allow_ref"):
                    adapter.allow_ref(supplier_ref)
                    adapter.allow_ref(catalog_ref)
        except Exception:
            pass
        self._emit("procurement.catalog_read_started", status="started")
        started = utc_now()
        try:
            result = invoke_tool_sync(
                self.tool_gateway,
                tool_id=TOOL_CATALOG_READ,
                operation=OP_CATALOG_READ,
                arguments={
                    "supplier_ref": supplier_ref,
                    "catalog_ref": catalog_ref,
                    "limit": limit or self.external_research_policy.catalog_max_items,
                },
            )
        except ProcurementError:
            self._emit("procurement.catalog_read_failed", status="failed")
            self._metric(
                "procurement_catalog_read_failure_total",
                status="failed",
                source_type="registered_catalog",
                tool_id=TOOL_CATALOG_READ,
            )
            raise
        data = dict(getattr(result, "data", None) or {})
        items = list(data.get("items") or [])
        offers = []
        if request_id:
            supplier_id = None
            for s in self.supplier_repo.list_for_scope(scope):
                if s.source_ref == supplier_ref or s.supplier_id == supplier_ref:
                    supplier_id = s.supplier_id
                    break
            supplier_id = supplier_id or f"ext-{supplier_ref[-12:]}"
            for idx, item in enumerate(items):
                currency = str(item.get("currency") or data.get("currency") or "").upper()
                if not currency or item.get("unit_price") is None:
                    continue
                prov_raw = dict(data.get("provenance") or {})
                provenance = OfferProvenance(
                    source_id=str(prov_raw.get("tool_id") or TOOL_CATALOG_READ),
                    source_ref=str(prov_raw.get("source_ref") or catalog_ref),
                    retrieved_at=utc_now(),
                    content_hash=str(prov_raw.get("content_hash") or "catalog"),
                    trust=str(prov_raw.get("trust_level") or "read_only_external"),
                    freshness=str(data.get("freshness") or "on_demand"),
                )
                offer = self.normalizer.from_document_row(
                    offer_id=f"cat-{request_id}-{idx}",
                    request_id=request_id,
                    supplier_id=supplier_id,
                    scope=scope,
                    row={
                        "currency": currency,
                        "unit_price": item.get("unit_price"),
                        "quantity": item.get("quantity_available"),
                        "specifications": item.get("specifications") or {},
                    },
                    provenance=provenance,
                    source_type="read_only_external",
                    source_ref=catalog_ref,
                )
                self.offer_repo.upsert(offer)
                offers.append(offer)
        elapsed = max(0, int((utc_now() - started).total_seconds() * 1000))
        self._emit(
            "procurement.catalog_read_completed",
            status="ok",
            metadata={"count": len(items)},
        )
        self._metric(
            "procurement_catalog_read_total",
            status="ok",
            source_type="registered_catalog",
            tool_id=TOOL_CATALOG_READ,
        )
        self._observe_latency(
            "procurement_external_latency_ms",
            elapsed,
            tool_id=TOOL_CATALOG_READ,
            source_type="registered_catalog",
        )
        return {"catalog": data, "offers": tuple(offers)}

    def prepare_rfq_draft(
        self,
        request_id: str,
        *,
        requesting_scope,
        supplier_ref: str,
        questions: tuple[str, ...] = (),
        language: str = "en",
    ) -> dict:
        req = self.get_request(request_id, requesting_scope=requesting_scope)
        if req is None:
            raise KeyError(request_id)
        self.access.require(requesting=requesting_scope, target=req.scope, operation=OP_READ)
        requirement = self.request_store.get_requirement(request_id)
        from procurement.adapters.gateway_bridge import invoke_tool_sync
        from procurement.adapters.models import OP_RFQ_DRAFT, TOOL_RFQ_DRAFT

        citations = self._knowledge_citations.get(request_id, ())
        try:
            result = invoke_tool_sync(
                self.tool_gateway,
                tool_id=TOOL_RFQ_DRAFT,
                operation=OP_RFQ_DRAFT,
                arguments={
                    "request_id": request_id,
                    "supplier_ref": supplier_ref,
                    "item_name": req.item_name,
                    "quantity": str(req.quantity) if req.quantity is not None else None,
                    "unit": req.unit,
                    "specs": dict(requirement.mandatory_specs) if requirement else dict(req.specifications),
                    "deadline": req.required_by.isoformat() if req.required_by else None,
                    "questions": list(questions),
                    "language": language,
                    "citations": list(citations),
                },
                capabilities=None,
            )
        except ProcurementError:
            raise
        data = dict(getattr(result, "data", None) or {})
        if not data.get("requires_human_send", True):
            data["requires_human_send"] = True
        data["external_send"] = False
        self._emit("procurement.rfq_draft_created", status="draft")
        self._metric(
            "procurement_rfq_draft_total",
            status="draft",
            source_type="rfq_draft",
            tool_id=TOOL_RFQ_DRAFT,
        )
        return data

    def send_rfq(self, *_a, **_k):
        raise ProcurementError("procurement_rfq_send_unsupported")

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

    def _metric(
        self,
        name: str,
        *,
        status: str,
        category: str = "general",
        tool_id: str | None = None,
        source_type: str | None = None,
    ) -> None:
        obs = self.observability
        if obs is None or not getattr(obs, "metrics", None):
            return
        try:
            labels = {
                "component": "procurement",
                "status": status,
            }
            if tool_id:
                labels["tool_id"] = tool_id
            if source_type:
                labels["source_type"] = source_type
            elif category:
                labels["case_type"] = category
            obs.metrics.inc(name, labels=labels)
        except Exception:
            pass

    def _observe_latency(
        self,
        name: str,
        duration_ms: float,
        *,
        tool_id: str,
        source_type: str = "external",
    ) -> None:
        obs = self.observability
        if obs is None or not getattr(obs, "metrics", None):
            return
        try:
            obs.metrics.observe_latency(
                name,
                float(duration_ms),
                labels={
                    "component": "procurement",
                    "tool_id": tool_id,
                    "status": "ok",
                    "source_type": source_type,
                },
            )
        except Exception:
            pass
