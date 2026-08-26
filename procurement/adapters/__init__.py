"""P17 procurement production adapters."""

from procurement.adapters.catalog_read import FakeCatalogBackend, SupplierCatalogReadAdapter
from procurement.adapters.descriptors import (
    catalog_read_tool_descriptor,
    procurement_adapter_schema_snapshot,
    rfq_draft_tool_descriptor,
    supplier_search_tool_descriptor,
)
from procurement.adapters.models import (
    PROCUREMENT_ADAPTER_SCHEMA_VERSION,
    PROCUREMENT_EXTERNAL_RESEARCH_POLICY_VERSION,
    PROCUREMENT_RFQ_DRAFT_VERSION,
    RfqDraft,
    SupplierCatalogResult,
    SupplierSearchResult,
)
from procurement.adapters.policy import ProcurementExternalResearchPolicy
from procurement.adapters.registry import build_offline_procurement_gateway, register_procurement_adapters
from procurement.adapters.rfq_draft import RfqDraftAdapter
from procurement.adapters.supplier_search import FakeSupplierSearchBackend, SupplierSearchAdapter

__all__ = [
    "FakeCatalogBackend",
    "FakeSupplierSearchBackend",
    "ProcurementExternalResearchPolicy",
    "PROCUREMENT_ADAPTER_SCHEMA_VERSION",
    "PROCUREMENT_EXTERNAL_RESEARCH_POLICY_VERSION",
    "PROCUREMENT_RFQ_DRAFT_VERSION",
    "RfqDraft",
    "RfqDraftAdapter",
    "SupplierCatalogReadAdapter",
    "SupplierCatalogResult",
    "SupplierSearchAdapter",
    "SupplierSearchResult",
    "build_offline_procurement_gateway",
    "catalog_read_tool_descriptor",
    "procurement_adapter_schema_snapshot",
    "register_procurement_adapters",
    "rfq_draft_tool_descriptor",
    "supplier_search_tool_descriptor",
]
