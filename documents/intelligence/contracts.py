"""Canonical contracts for Files & Document Intelligence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id

CONF_EXACT = "exact"
CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"
CONF_UNRESOLVED = "unresolved"
CONFIDENCE_LEVELS = (CONF_EXACT, CONF_HIGH, CONF_MEDIUM, CONF_LOW, CONF_UNRESOLVED)

BIZ_CONTRACT = "contract"
BIZ_INVOICE = "invoice"
BIZ_ACT = "act"
BIZ_WAYBILL = "waybill"
BIZ_PRICE_LIST = "price_list"
BIZ_STATEMENT = "statement"
BIZ_GENERIC = "generic_document"
BUSINESS_DOC_TYPES = (
    BIZ_CONTRACT,
    BIZ_INVOICE,
    BIZ_ACT,
    BIZ_WAYBILL,
    BIZ_PRICE_LIST,
    BIZ_STATEMENT,
    BIZ_GENERIC,
)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DocumentDescriptor:
    document_id: str
    tenant_id: str
    filename: str
    media_type: str
    document_type: str
    size: int
    checksum: str
    source_ref: str = ""
    created_at: datetime = field(default_factory=utc_now)
    provenance: Mapping[str, object] = field(default_factory=dict)
    business_type: str = BIZ_GENERIC
    status: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "provenance", _meta(self.provenance))


@dataclass(frozen=True)
class DocumentContent:
    document_id: str
    text: str
    pages: tuple[Mapping[str, object], ...] = ()
    sections: tuple[Mapping[str, object], ...] = ()
    tables: tuple[Mapping[str, object], ...] = ()
    image_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    extraction_method: str = "parser"
    confidence: str = CONF_MEDIUM
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "pages", tuple(dict(p) for p in self.pages))
        object.__setattr__(self, "sections", tuple(dict(s) for s in self.sections))
        object.__setattr__(self, "tables", tuple(dict(t) for t in self.tables))
        object.__setattr__(self, "image_refs", tuple(self.image_refs or ()))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))
        if self.confidence not in CONFIDENCE_LEVELS:
            object.__setattr__(self, "confidence", CONF_MEDIUM)


@dataclass(frozen=True)
class ExtractedField:
    name: str
    value: object
    source_ref: str = ""
    confidence: str = CONF_MEDIUM
    method: str = "rule"

    def __post_init__(self):
        if self.confidence not in CONFIDENCE_LEVELS:
            object.__setattr__(self, "confidence", CONF_MEDIUM)


@dataclass(frozen=True)
class StructuredDocument:
    document_id: str
    document_type: str
    schema_version: str
    fields: Mapping[str, object]
    line_items: tuple[Mapping[str, object], ...] = ()
    parties: tuple[Mapping[str, object], ...] = ()
    dates: Mapping[str, object] = field(default_factory=dict)
    amounts: Mapping[str, object] = field(default_factory=dict)
    identifiers: Mapping[str, object] = field(default_factory=dict)
    confidence: str = CONF_MEDIUM
    validation_ok: bool = True
    validation_errors: tuple[str, ...] = ()
    provenance: Mapping[str, object] = field(default_factory=dict)
    field_evidence: tuple[ExtractedField, ...] = ()

    def __post_init__(self):
        if self.document_type not in BUSINESS_DOC_TYPES:
            object.__setattr__(self, "document_type", BIZ_GENERIC)
        object.__setattr__(self, "fields", _meta(self.fields))
        object.__setattr__(self, "line_items", tuple(dict(x) for x in self.line_items))
        object.__setattr__(self, "parties", tuple(dict(x) for x in self.parties))
        object.__setattr__(self, "dates", _meta(self.dates))
        object.__setattr__(self, "amounts", _meta(self.amounts))
        object.__setattr__(self, "identifiers", _meta(self.identifiers))
        object.__setattr__(self, "validation_errors", tuple(self.validation_errors or ()))
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "field_evidence", tuple(self.field_evidence or ()))
        if self.confidence not in CONFIDENCE_LEVELS:
            object.__setattr__(self, "confidence", CONF_MEDIUM)


@dataclass(frozen=True)
class DocumentComparisonResult:
    left_ref: str
    right_ref: str
    changed_fields: tuple[Mapping[str, object], ...] = ()
    added_sections: tuple[str, ...] = ()
    removed_sections: tuple[str, ...] = ()
    table_differences: tuple[Mapping[str, object], ...] = ()
    summary: Mapping[str, object] = field(default_factory=dict)
    unchanged: bool = False

    def __post_init__(self):
        object.__setattr__(self, "changed_fields", tuple(dict(x) for x in self.changed_fields))
        object.__setattr__(self, "added_sections", tuple(self.added_sections or ()))
        object.__setattr__(self, "removed_sections", tuple(self.removed_sections or ()))
        object.__setattr__(
            self, "table_differences", tuple(dict(x) for x in self.table_differences)
        )
        object.__setattr__(self, "summary", _meta(self.summary))


@dataclass(frozen=True)
class DocumentLinkResult:
    left_ref: str
    right_ref: str
    link_type: str
    confidence: str
    evidence: tuple[str, ...] = ()
    same_related: bool = False

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(self.evidence or ()))
        if self.confidence not in CONFIDENCE_LEVELS:
            object.__setattr__(self, "confidence", CONF_UNRESOLVED)


@dataclass(frozen=True)
class GeneratedDocument:
    document_id: str
    tenant_id: str
    media_type: str
    filename: str
    content: bytes
    template_id: str
    template_version: str
    checksum: str
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "content", bytes(self.content))
        object.__setattr__(self, "provenance", _meta(self.provenance))
