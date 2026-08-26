"""ProcurementRuntime composition — offline-first, no network on startup."""

from __future__ import annotations

import os

from procurement.access import ProcurementAccessPolicy
from procurement.comparator import SupplierComparator
from procurement.discovery import SupplierDiscoveryService
from procurement.errors import ProcurementError
from procurement.normalizer import OfferNormalizer
from procurement.policy import ProcurementPolicy, ProcurementScoringPolicy
from procurement.recommendation import ProcurementRecommendationService
from procurement.repos import InMemoryOfferRepository, InMemoryRequestStore, InMemorySupplierRepository
from procurement.risk import ProcurementRiskAnalyzer
from procurement.service import ProcurementService
from procurement.sqlite_store import (
    ProcurementPersistenceUnavailableError,
    SqliteOfferRepository,
    SqliteProcurementStore,
    SqliteSupplierRepository,
)
from procurement.validator import ProcurementValidator
from procurement.workflow import ProcurementWorkflow
from security.encryption import EncryptionService


def procurement_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("PROCUREMENT_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def procurement_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    return {
        "enabled": procurement_enabled(source),
        "backend": str(source.get("PROCUREMENT_BACKEND", "memory") or "memory").strip().lower(),
        "db_path": str(source.get("PROCUREMENT_DB_PATH") or "").strip() or None,
        "minimum_valid_offers": int(source.get("PROCUREMENT_MIN_VALID_OFFERS", "2") or 2),
    }


def build_procurement_store(
    *,
    env: dict | None = None,
    shared_connection=None,
    encryption: EncryptionService | None = None,
    require_durable: bool = False,
):
    cfg = procurement_config(env)
    backend = cfg["backend"]
    if backend in {"sqlite", "durable"} or cfg["db_path"]:
        path = cfg["db_path"]
        if shared_connection is not None and not path:
            return SqliteProcurementStore(
                shared_connection=shared_connection,
                owns_connection=False,
                encryption=encryption,
            )
        if path:
            return SqliteProcurementStore(db_path=path, owns_connection=True, encryption=encryption)
        if require_durable or backend in {"sqlite", "durable"}:
            raise ProcurementPersistenceUnavailableError("procurement_store_unavailable")
        if shared_connection is not None:
            return SqliteProcurementStore(
                shared_connection=shared_connection,
                owns_connection=False,
                encryption=encryption,
            )
        raise ProcurementPersistenceUnavailableError("procurement_store_unavailable")
    if require_durable:
        raise ProcurementPersistenceUnavailableError("procurement_store_unavailable")
    return None


class ProcurementRuntime:
    def __init__(
        self,
        *,
        service: ProcurementService,
        workflow: ProcurementWorkflow,
        policy: ProcurementPolicy,
        validator: ProcurementValidator,
        supplier_repo,
        offer_repo,
        discovery: SupplierDiscoveryService,
        normalizer: OfferNormalizer,
        comparator: SupplierComparator,
        risk_analyzer: ProcurementRiskAnalyzer,
        recommendation_service: ProcurementRecommendationService,
        store=None,
        enabled: bool = True,
    ):
        self.service = service
        self.workflow = workflow
        self.policy = policy
        self.validator = validator
        self.supplier_repo = supplier_repo
        self.offer_repo = offer_repo
        self.discovery = discovery
        self.normalizer = normalizer
        self.comparator = comparator
        self.risk_analyzer = risk_analyzer
        self.recommendation_service = recommendation_service
        self.store = store
        self.enabled = bool(enabled)

    def health(self) -> dict:
        ready = bool(getattr(self.store, "available", True)) if self.store is not None else True
        if self.store is None:
            # in-memory
            ready = True
            backend = "memory"
            mode = "memory"
        else:
            backend = getattr(self.store, "persistence_backend", "sqlite")
            mode = getattr(self.store, "connection_mode", "unknown")
        status = "healthy"
        if self.enabled and self.service.blocked_reason:
            status = "blocked"
        elif self.enabled and not ready:
            status = "blocked"
        elif self.service.knowledge_service is None and self.service.document_service is None:
            status = "degraded"
        return {
            "procurement_status": status,
            "enabled": self.enabled,
            "persistence_ready": ready and not bool(self.service.blocked_reason),
            "persistence_backend": backend,
            "connection_mode": mode,
            "workflow_version": self.workflow.workflow_version,
        }

    def close(self) -> None:
        if self.store is not None and hasattr(self.store, "close"):
            try:
                self.store.close()
            except Exception:
                pass


def build_procurement_runtime(
    *,
    env: dict | None = None,
    knowledge_service=None,
    document_service=None,
    memory_service=None,
    workflow_engine=None,
    tool_gateway=None,
    autonomy_gate=None,
    hitl_service=None,
    observability=None,
    shared_connection=None,
    encryption: EncryptionService | None = None,
) -> ProcurementRuntime | None:
    _ = tool_gateway
    cfg = procurement_config(env)
    if not cfg["enabled"]:
        return None

    policy = ProcurementPolicy(minimum_valid_offers=cfg["minimum_valid_offers"])
    scoring = ProcurementScoringPolicy()
    access = ProcurementAccessPolicy()
    validator = ProcurementValidator()
    blocked_reason = None
    store = None
    request_store = None
    supplier_repo = None
    offer_repo = None

    try:
        store = build_procurement_store(
            env=env,
            shared_connection=shared_connection,
            encryption=encryption,
            require_durable=cfg["backend"] in {"sqlite", "durable"} or bool(cfg["db_path"]),
        )
    except ProcurementPersistenceUnavailableError as exc:
        if cfg["backend"] in {"sqlite", "durable"} or cfg["db_path"]:
            # Configured durable backend must not silently fall back to memory.
            blocked_reason = exc.reason
            request_store = InMemoryRequestStore()
            supplier_repo = InMemorySupplierRepository()
            offer_repo = InMemoryOfferRepository()
            store = None
        else:
            raise
    except Exception as exc:
        if cfg["backend"] in {"sqlite", "durable"} or cfg["db_path"]:
            blocked_reason = "procurement_store_unavailable"
            request_store = InMemoryRequestStore()
            supplier_repo = InMemorySupplierRepository()
            offer_repo = InMemoryOfferRepository()
            store = None
        else:
            raise ProcurementError("procurement_store_unavailable") from exc

    if blocked_reason is None:
        if store is None:
            request_store = InMemoryRequestStore()
            supplier_repo = InMemorySupplierRepository()
            offer_repo = InMemoryOfferRepository()
        else:
            request_store = store
            supplier_repo = SqliteSupplierRepository(store)
            offer_repo = SqliteOfferRepository(store)

    normalizer = OfferNormalizer()
    comparator = SupplierComparator(scoring)
    risk_analyzer = ProcurementRiskAnalyzer()
    recommendation_service = ProcurementRecommendationService(
        comparator=comparator,
        risk_analyzer=risk_analyzer,
        validator=validator,
        policy=policy,
    )
    discovery = SupplierDiscoveryService(
        supplier_repo=supplier_repo,
        knowledge_service=knowledge_service,
        document_service=document_service,
    )
    service = ProcurementService(
        policy=policy,
        access=access,
        validator=validator,
        request_store=request_store,
        supplier_repo=supplier_repo,
        offer_repo=offer_repo,
        discovery=discovery,
        normalizer=normalizer,
        comparator=comparator,
        risk_analyzer=risk_analyzer,
        recommendation_service=recommendation_service,
        knowledge_service=knowledge_service,
        document_service=document_service,
        memory_service=memory_service,
        workflow_engine=workflow_engine,
        autonomy_gate=autonomy_gate,
        hitl_service=hitl_service,
        observability=observability,
        enabled=True,
    )
    if blocked_reason:
        service.blocked_reason = blocked_reason
    workflow = ProcurementWorkflow(service)
    return ProcurementRuntime(
        service=service,
        workflow=workflow,
        policy=policy,
        validator=validator,
        supplier_repo=supplier_repo,
        offer_repo=offer_repo,
        discovery=discovery,
        normalizer=normalizer,
        comparator=comparator,
        risk_analyzer=risk_analyzer,
        recommendation_service=recommendation_service,
        store=store,
        enabled=True,
    )
