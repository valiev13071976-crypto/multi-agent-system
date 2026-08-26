"""Canonical P17 procurement adapter models and versions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from memory.models import utc_now
from procurement.models import TRUST_EXTERNAL, TRUST_UNVERIFIED

PROCUREMENT_ADAPTER_SCHEMA_VERSION = "1.0.0"
PROCUREMENT_EXTERNAL_RESEARCH_POLICY_VERSION = "1.0.0"
PROCUREMENT_RFQ_DRAFT_VERSION = "1.0.0"

TOOL_SUPPLIER_SEARCH = "procurement.supplier_search"
TOOL_CATALOG_READ = "procurement.catalog_read"
TOOL_RFQ_DRAFT = "procurement.rfq_draft"

OP_SEARCH = "search_suppliers"
OP_CATALOG_READ = "read_catalog"
OP_RFQ_DRAFT = "prepare_draft"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


@dataclass(frozen=True)
class SupplierSearchResult:
    supplier_name: str
    supplier_ref: str
    source: str
    retrieved_at: datetime
    trust_level: str
    provenance: Mapping[str, object]
    categories: tuple[str, ...] = ()
    country: str | None = None
    website_ref: str | None = None
    snippet_safe: str = ""
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.trust_level not in {TRUST_EXTERNAL, TRUST_UNVERIFIED}:
            object.__setattr__(self, "trust_level", TRUST_UNVERIFIED)
        object.__setattr__(self, "categories", tuple(self.categories or ()))
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))

    def as_dict(self) -> dict:
        return {
            "supplier_name": self.supplier_name,
            "supplier_ref": self.supplier_ref,
            "country": self.country,
            "website_ref": self.website_ref,
            "categories": list(self.categories),
            "snippet_safe": self.snippet_safe,
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat(),
            "trust_level": self.trust_level,
            "provenance": dict(self.provenance),
            "metadata_safe": dict(self.metadata_safe),
        }


@dataclass(frozen=True)
class CatalogItem:
    name: str
    sku: str | None = None
    unit_price: str | None = None
    currency: str | None = None
    quantity_available: str | None = None
    specifications: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "specifications", _meta(self.specifications))

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "unit_price": self.unit_price,
            "currency": self.currency,
            "quantity_available": self.quantity_available,
            "specifications": dict(self.specifications),
        }


@dataclass(frozen=True)
class SupplierCatalogResult:
    supplier_ref: str
    source_ref: str
    items: tuple[CatalogItem, ...]
    retrieved_at: datetime
    freshness: str
    provenance: Mapping[str, object]
    currency: str | None = None
    availability: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "items", tuple(self.items or ()))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))
        object.__setattr__(self, "provenance", _meta(self.provenance))

    def as_dict(self) -> dict:
        return {
            "supplier_ref": self.supplier_ref,
            "source_ref": self.source_ref,
            "items": [i.as_dict() for i in self.items],
            "currency": self.currency,
            "availability": self.availability,
            "retrieved_at": self.retrieved_at.isoformat(),
            "freshness": self.freshness,
            "provenance": dict(self.provenance),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RfqDraft:
    subject: str
    body: str
    supplier_ref: str
    request_id: str
    citations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    requires_human_send: bool = True
    draft_version: str = PROCUREMENT_RFQ_DRAFT_VERSION
    created_at: datetime = field(default_factory=utc_now)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "citations", tuple(self.citations or ()))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))
        object.__setattr__(self, "requires_human_send", True)
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))

    def as_dict(self) -> dict:
        return {
            "subject": self.subject,
            "body": self.body,
            "supplier_ref": self.supplier_ref,
            "request_id": self.request_id,
            "citations": list(self.citations),
            "warnings": list(self.warnings),
            "requires_human_send": True,
            "draft_version": self.draft_version,
            "created_at": self.created_at.isoformat(),
            "metadata_safe": dict(self.metadata_safe),
        }
