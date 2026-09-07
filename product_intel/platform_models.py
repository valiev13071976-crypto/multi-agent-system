"""Block 5.5 — canonical Product / Catalog Intelligence domain contracts.

Vendor-neutral internal product model. Consumed by future CRM/marketplace/
ERP adapters through the governed Block 5.4 Tool & Integration Platform --
this module itself must never contain vendor-specific business logic (spec
section 19/31).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import require_tenant_id

SCHEMA_VERSION = "1.0.0"

# --- Provenance (spec section 16) ------------------------------------------
PROV_SOURCE = "SOURCE"
PROV_NORMALIZED = "NORMALIZED"
PROV_DERIVED = "DERIVED"
PROV_GENERATED = "GENERATED"
PROV_USER_CONFIRMED = "USER_CONFIRMED"
PROVENANCE_LEVELS = (PROV_SOURCE, PROV_NORMALIZED, PROV_DERIVED, PROV_GENERATED, PROV_USER_CONFIRMED)

# --- Validation state (spec section 15) -------------------------------------
VALIDATION_UNVALIDATED = "UNVALIDATED"
VALIDATION_VALID = "VALID"
VALIDATION_WARNING = "WARNING"
VALIDATION_INVALID = "INVALID"
VALIDATION_STATES = (VALIDATION_UNVALIDATED, VALIDATION_VALID, VALIDATION_WARNING, VALIDATION_INVALID)

# --- Matching state (spec section 7) -----------------------------------------
MATCH_STATE_NEW = "NEW"
MATCH_STATE_EXACT = "EXACT"
MATCH_STATE_HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
MATCH_STATE_AMBIGUOUS = "AMBIGUOUS"
MATCH_STATE_NO_MATCH = "NO_MATCH"
MATCH_STATES = (
    MATCH_STATE_NEW,
    MATCH_STATE_EXACT,
    MATCH_STATE_HIGH_CONFIDENCE,
    MATCH_STATE_AMBIGUOUS,
    MATCH_STATE_NO_MATCH,
)

# --- Stock availability -------------------------------------------------------
AVAIL_IN_STOCK = "IN_STOCK"
AVAIL_OUT_OF_STOCK = "OUT_OF_STOCK"
AVAIL_LOW_STOCK = "LOW_STOCK"
AVAIL_UNKNOWN = "UNKNOWN"

# --- Identifier types ----------------------------------------------------------
IDENT_INTERNAL = "internal_id"
IDENT_GTIN = "gtin"
IDENT_EAN = "ean"
IDENT_SKU = "sku"
IDENT_ARTICLE = "article"
IDENT_MPN = "mpn"

# --- Source types ---------------------------------------------------------------
SOURCE_EXCEL = "excel"
SOURCE_ACQUISITION = "acquisition"
SOURCE_ARTIFACT = "artifact"
SOURCE_PAYLOAD = "payload"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def _str_map(value) -> Mapping[str, str]:
    return MappingProxyType({str(k): str(v) for k, v in dict(value or {}).items()})


@dataclass(frozen=True)
class Identifier:
    id_type: str
    value: str
    trust: str = PROV_SOURCE


@dataclass(frozen=True)
class PriceInfo:
    currency: str = ""
    purchase_price: Decimal | None = None
    selling_price: Decimal | None = None
    previous_price: Decimal | None = None
    source: str = ""
    observed_at: datetime = field(default_factory=_utc)


@dataclass(frozen=True)
class StockInfo:
    quantity: Decimal | None = None
    availability: str = AVAIL_UNKNOWN
    source: str = ""
    observed_at: datetime = field(default_factory=_utc)


@dataclass(frozen=True)
class SourceReference:
    source_type: str
    source_ref: str = ""
    row_ref: str = ""
    raw_snapshot: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "raw_snapshot", _meta(self.raw_snapshot))


@dataclass(frozen=True)
class Category:
    category_id: str
    tenant_id: str
    name: str
    source_category: str = ""
    canonical_path: tuple[str, ...] = ()
    ambiguous: bool = False

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "canonical_path", tuple(self.canonical_path or ()))


@dataclass(frozen=True)
class Brand:
    brand_id: str
    tenant_id: str
    name: str
    normalized_name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "aliases", tuple(self.aliases or ()))


@dataclass(frozen=True)
class ProductVariant:
    """Explicit variant/SKU sub-entity of a base Product (spec section 3).

    Ingestion normally produces one flat ``Product`` per source row (see
    ``product_intel.service``'s docstring for the rationale); this type is
    used when a caller explicitly groups sibling SKUs under a parent
    product without collapsing their distinct identity.
    """

    variant_id: str
    product_id: str
    sku: str
    attributes: Mapping[str, str] = field(default_factory=dict)
    gtin: str = ""
    price: PriceInfo | None = None
    stock: StockInfo | None = None

    def __post_init__(self):
        object.__setattr__(self, "attributes", _str_map(self.attributes))


@dataclass(frozen=True)
class ProductValidationIssue:
    code: str
    severity: str
    message: str
    field: str = ""


@dataclass(frozen=True)
class ProductValidationResult:
    product_id: str
    tenant_id: str
    state: str
    issues: tuple[ProductValidationIssue, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "issues", tuple(self.issues or ()))
        if self.state not in VALIDATION_STATES:
            object.__setattr__(self, "state", VALIDATION_INVALID)


@dataclass(frozen=True)
class ProductMatchCandidate:
    product_id: str
    confidence: str
    method: str
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "evidence", _meta(self.evidence))


@dataclass(frozen=True)
class ProductMatchOutcome:
    state: str
    method: str
    matched_product_id: str = ""
    candidates: tuple[ProductMatchCandidate, ...] = ()
    conflicts: tuple[str, ...] = ()
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "candidates", tuple(self.candidates or ()))
        object.__setattr__(self, "conflicts", tuple(self.conflicts or ()))
        object.__setattr__(self, "evidence", _meta(self.evidence))
        if self.state not in MATCH_STATES:
            object.__setattr__(self, "state", MATCH_STATE_NO_MATCH)


@dataclass(frozen=True)
class Product:
    product_id: str
    tenant_id: str
    owner_id: str = ""

    title: str = ""
    normalized_title: str = ""

    brand: str = ""
    normalized_brand: str = ""

    category: str = ""
    category_path: tuple[str, ...] = ()

    sku: str = ""
    article: str = ""
    gtin: str = ""
    mpn: str = ""

    description: str = ""
    short_description: str = ""
    seo_title: str = ""
    seo_description: str = ""
    keywords: tuple[str, ...] = ()

    attributes: Mapping[str, str] = field(default_factory=dict)
    variant_attributes: Mapping[str, str] = field(default_factory=dict)
    variant_group_key: str = ""

    price: PriceInfo = field(default_factory=PriceInfo)
    stock: StockInfo = field(default_factory=StockInfo)

    media_refs: tuple[str, ...] = ()
    content_refs: tuple[str, ...] = ()

    source: SourceReference | None = None
    field_provenance: Mapping[str, str] = field(default_factory=dict)

    validation_state: str = VALIDATION_UNVALIDATED
    matching_state: str = MATCH_STATE_NEW

    created_at: datetime = field(default_factory=_utc)
    updated_at: datetime = field(default_factory=_utc)
    version: int = 1
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "category_path", tuple(self.category_path or ()))
        object.__setattr__(self, "keywords", tuple(self.keywords or ()))
        object.__setattr__(self, "attributes", _str_map(self.attributes))
        object.__setattr__(self, "variant_attributes", _str_map(self.variant_attributes))
        object.__setattr__(self, "media_refs", tuple(self.media_refs or ()))
        object.__setattr__(self, "content_refs", tuple(self.content_refs or ()))
        object.__setattr__(self, "field_provenance", _str_map(self.field_provenance))
        if self.validation_state not in VALIDATION_STATES:
            object.__setattr__(self, "validation_state", VALIDATION_UNVALIDATED)
        if self.matching_state not in MATCH_STATES:
            object.__setattr__(self, "matching_state", MATCH_STATE_NEW)


@dataclass(frozen=True)
class ProductImportResult:
    import_id: str
    tenant_id: str
    catalog_id: str
    total_rows: int = 0
    created: int = 0
    updated: int = 0
    invalid: int = 0
    ambiguous: int = 0
    product_ids: tuple[str, ...] = ()
    details: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "product_ids", tuple(self.product_ids or ()))
        object.__setattr__(self, "details", tuple(_meta(d) for d in (self.details or ())))


@dataclass(frozen=True)
class ProductDuplicateGroup:
    tenant_id: str
    product_ids: tuple[str, ...]
    state: str
    method: str
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "product_ids", tuple(self.product_ids or ()))
        object.__setattr__(self, "evidence", _meta(self.evidence))
