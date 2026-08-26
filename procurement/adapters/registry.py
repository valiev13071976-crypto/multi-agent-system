"""Register P17 procurement adapters into ToolRegistry (before freeze)."""

from __future__ import annotations

from procurement.adapters.catalog_read import FakeCatalogBackend, SupplierCatalogReadAdapter
from procurement.adapters.descriptors import (
    catalog_read_tool_descriptor,
    rfq_draft_tool_descriptor,
    supplier_search_tool_descriptor,
)
from procurement.adapters.policy import ProcurementExternalResearchPolicy
from procurement.adapters.rfq_draft import RfqDraftAdapter
from procurement.adapters.supplier_search import FakeSupplierSearchBackend, SupplierSearchAdapter
from tools.gateway import ToolGateway
from tools.registry import ToolRegistry


def register_procurement_adapters(
    registry: ToolRegistry,
    *,
    policy: ProcurementExternalResearchPolicy | None = None,
    search_backend=None,
    catalog_backend=None,
    search_adapter: SupplierSearchAdapter | None = None,
    catalog_adapter: SupplierCatalogReadAdapter | None = None,
    rfq_adapter: RfqDraftAdapter | None = None,
) -> dict:
    """Register bounded procurement tools. Registry must not be frozen yet."""

    policy = policy or ProcurementExternalResearchPolicy()
    search = search_adapter or SupplierSearchAdapter(
        backend=search_backend if search_backend is not None else FakeSupplierSearchBackend(),
        enabled=policy.enabled,
        max_results=policy.max_results_per_query,
    )
    catalog = catalog_adapter or SupplierCatalogReadAdapter(
        backend=catalog_backend if catalog_backend is not None else FakeCatalogBackend(),
        max_items=policy.catalog_max_items,
        enabled=True,
    )
    rfq = rfq_adapter or RfqDraftAdapter(max_chars=policy.rfq_draft_max_chars, enabled=True)
    registry.register(
        supplier_search_tool_descriptor(
            enabled=policy.enabled, timeout_seconds=policy.timeout_seconds
        ),
        adapter=search,
    )
    registry.register(
        catalog_read_tool_descriptor(enabled=True, timeout_seconds=policy.timeout_seconds),
        adapter=catalog,
    )
    registry.register(
        rfq_draft_tool_descriptor(enabled=True, timeout_seconds=min(5.0, policy.timeout_seconds)),
        adapter=rfq,
    )
    return {
        "supplier_search": search,
        "catalog_read": catalog,
        "rfq_draft": rfq,
    }


def build_offline_procurement_gateway(
    *,
    policy: ProcurementExternalResearchPolicy | None = None,
    search_backend=None,
    catalog_backend=None,
    observability=None,
) -> tuple[ToolRegistry, ToolGateway, dict]:
    """Deterministic offline ToolGateway with P17 adapters (no live network)."""

    policy = policy or ProcurementExternalResearchPolicy()
    registry = ToolRegistry()
    adapters = register_procurement_adapters(
        registry,
        policy=policy,
        search_backend=search_backend,
        catalog_backend=catalog_backend,
    )
    gateway = ToolGateway(
        registry=registry,
        register_search=True,
        observability=observability,
    )
    registry.freeze()
    gateway._procurement_adapters = adapters  # noqa: SLF001 — test/runtime hook
    return registry, gateway, adapters
