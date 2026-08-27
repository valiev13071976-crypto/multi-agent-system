"""Canonical immutable contracts for Excel / Data Intelligence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id

SCHEMA_VERSION = "1.0.0"

CONF_EXACT = "exact"
CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"
CONF_UNRESOLVED = "unresolved"
CONF_CONFLICT = "conflict"
CONFIDENCE_LEVELS = (
    CONF_EXACT,
    CONF_HIGH,
    CONF_MEDIUM,
    CONF_LOW,
    CONF_UNRESOLVED,
    CONF_CONFLICT,
)

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"

TYPE_STRING = "string"
TYPE_INTEGER = "integer"
TYPE_DECIMAL = "decimal"
TYPE_CURRENCY = "currency"
TYPE_PERCENT = "percent"
TYPE_DATE = "date"
TYPE_DATETIME = "datetime"
TYPE_BOOLEAN = "boolean"
TYPE_IDENTIFIER = "identifier"
TYPE_CATEGORICAL = "categorical"

ROLE_COMPANY_NAME = "company_name"
ROLE_INN = "inn"
ROLE_KPP = "kpp"
ROLE_OGRN = "ogrn"
ROLE_COUNTERPARTY = "counterparty"
ROLE_SKU = "sku"
ROLE_ARTICLE = "article"
ROLE_EAN = "ean"
ROLE_MPN = "mpn"
ROLE_PRODUCT_NAME = "product_name"
ROLE_QUANTITY = "quantity"
ROLE_UNIT = "unit"
ROLE_PRICE = "price"
ROLE_CURRENCY = "currency"
ROLE_STOCK = "stock"
ROLE_AMOUNT = "amount"
ROLE_VAT_RATE = "vat_rate"
ROLE_VAT_AMOUNT = "vat_amount"
ROLE_PAYMENT_DATE = "payment_date"
ROLE_DOCUMENT_NUMBER = "document_number"
ROLE_WAREHOUSE = "warehouse"
ROLE_SUPPLIER = "supplier"
ROLE_UNKNOWN = "unknown"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4()}"


def row_ref(dataset_id: str, table_id: str, source_row: int) -> str:
    return f"{dataset_id}:{table_id}:r{int(source_row)}"


@dataclass(frozen=True)
class ColumnDescriptor:
    source_name: str
    normalized_name: str
    inferred_type: str = TYPE_STRING
    semantic_role: str = ROLE_UNKNOWN
    nullable: bool = True
    confidence: str = CONF_MEDIUM
    examples_safe: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "examples_safe", tuple(self.examples_safe or ()))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        if self.confidence not in CONFIDENCE_LEVELS:
            object.__setattr__(self, "confidence", CONF_MEDIUM)


@dataclass(frozen=True)
class TableDescriptor:
    table_id: str
    sheet: str
    range: str
    header_row: int
    columns: tuple[ColumnDescriptor, ...]
    row_count: int
    confidence: str = CONF_MEDIUM
    evidence: Mapping[str, object] = field(default_factory=dict)
    unresolved: bool = False

    def __post_init__(self):
        object.__setattr__(self, "columns", tuple(self.columns or ()))
        object.__setattr__(self, "evidence", _meta(self.evidence))
        if self.confidence not in CONFIDENCE_LEVELS:
            object.__setattr__(self, "confidence", CONF_MEDIUM)


@dataclass(frozen=True)
class DatasetDescriptor:
    dataset_id: str
    tenant_id: str
    source_document_id: str
    format: str
    sheets: tuple[str, ...]
    tables: tuple[TableDescriptor, ...]
    row_count: int
    column_count: int
    checksum: str
    schema_version: str = SCHEMA_VERSION
    created_at: datetime = field(default_factory=utc_now)
    provenance: Mapping[str, object] = field(default_factory=dict)
    status: str = "ready"

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "sheets", tuple(self.sheets or ()))
        object.__setattr__(self, "tables", tuple(self.tables or ()))
        object.__setattr__(self, "provenance", _meta(self.provenance))


@dataclass(frozen=True)
class DataIssue:
    row_ref: str
    column: str
    issue_type: str
    severity: str
    description: str
    suggested_action: str = ""
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "evidence", _meta(self.evidence))
        if self.severity not in {SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_ERROR}:
            object.__setattr__(self, "severity", SEVERITY_WARNING)


@dataclass(frozen=True)
class MatchResult:
    entity_type: str
    left_ref: str
    right_ref: str
    match_method: str
    confidence: str
    evidence: Mapping[str, object] = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()
    same_entity: bool = False
    review_required: bool = False

    def __post_init__(self):
        object.__setattr__(self, "evidence", _meta(self.evidence))
        object.__setattr__(self, "conflicts", tuple(self.conflicts or ()))
        if self.confidence not in CONFIDENCE_LEVELS:
            object.__setattr__(self, "confidence", CONF_UNRESOLVED)


@dataclass(frozen=True)
class DataTransformation:
    operation: str
    input_refs: tuple[str, ...]
    output_ref: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    provenance: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        object.__setattr__(self, "input_refs", tuple(self.input_refs or ()))
        object.__setattr__(self, "parameters", _meta(self.parameters))
        object.__setattr__(self, "provenance", _meta(self.provenance))


@dataclass(frozen=True)
class DataRow:
    """Normalized row with lineage to source."""

    row_id: str
    dataset_id: str
    table_id: str
    source_row: int
    values: Mapping[str, object]
    raw_values: Mapping[str, object] = field(default_factory=dict)
    roles: Mapping[str, str] = field(default_factory=dict)
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "values", _meta(self.values))
        object.__setattr__(self, "raw_values", _meta(self.raw_values))
        object.__setattr__(self, "roles", _meta(self.roles))
        object.__setattr__(self, "provenance", _meta(self.provenance))

    @property
    def ref(self) -> str:
        return row_ref(self.dataset_id, self.table_id, self.source_row)
