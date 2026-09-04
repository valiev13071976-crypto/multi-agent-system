"""Canonical Product Content contracts (Blocks 12–14). Reuses provenance vocabulary from Block 11."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from data_intel.economics import PROV_CONFIGURED, PROV_DERIVED, PROV_FILE, PROV_UNKNOWN, PROV_USER

PROV_CATALOG = "CATALOG_PROVIDED"
PROV_AI = "AI_GENERATED"

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
INSUFFICIENT_INPUT = "INSUFFICIENT_INPUT"
REQUIRES_REVIEW = "REQUIRES_REVIEW"

STATUS_READY = "READY"
STATUS_READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
STATUS_REQUIRES_REVIEW = "REQUIRES_REVIEW"
STATUS_BLOCKED = "BLOCKED"

SOURCE_IMAGE = "SOURCE_IMAGE"
DERIVED_IMAGE = "DERIVED_IMAGE"
AI_GENERATED_IMAGE = "AI_GENERATED_IMAGE"

ROLE_MAIN = "MAIN"
ROLE_GALLERY = "GALLERY"
ROLE_DETAIL = "DETAIL"
ROLE_LIFESTYLE = "LIFESTYLE"
ROLE_PACKAGING = "PACKAGING"
ROLE_INFOGRAPHIC = "INFOGRAPHIC"
ROLE_THUMBNAIL = "THUMBNAIL"

DEFAULT_ROLE_ORDER = (
    ROLE_MAIN,
    ROLE_DETAIL,
    ROLE_GALLERY,
    ROLE_LIFESTYLE,
    ROLE_INFOGRAPHIC,
    ROLE_PACKAGING,
    ROLE_THUMBNAIL,
)

POLICY_VERSION = "product-content-a-1.0.0"


def provenance_label(source: str | None) -> str:
    raw = str(source or "").strip().upper()
    mapping = {
        "FILE": PROV_FILE,
        "FILE_PROVIDED": PROV_FILE,
        "USER": PROV_USER,
        "USER_PROVIDED": PROV_USER,
        "CATALOG": PROV_CATALOG,
        "CATALOG_PROVIDED": PROV_CATALOG,
        "CONFIGURED": PROV_CONFIGURED,
        "DERIVED": PROV_DERIVED,
        "AI": PROV_AI,
        "AI_GENERATED": PROV_AI,
        "UNKNOWN": PROV_UNKNOWN,
    }
    return mapping.get(raw, PROV_UNKNOWN if not raw else raw)


@dataclass(frozen=True)
class ProvenancedValue:
    raw: str | None
    normalized: str | None
    provenance: str
    role: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProductCard:
    tenant_id: str
    product_id: str
    sku: str
    article: str = ""
    barcode: str = ""
    brand: str = ""
    model: str = ""
    category: str = "generic"
    subcategory: str = ""
    product_name: str = ""
    canonical_title: str = ""
    short_description: str = ""
    long_description: str = ""
    feature_bullets: tuple[str, ...] = ()
    attributes: tuple[ProvenancedValue, ...] = ()
    specifications: dict[str, ProvenancedValue] = field(default_factory=dict)
    dimensions: str = ""
    weight: str = ""
    materials: str = ""
    color: str = ""
    variant: str = ""
    country_of_origin: str = ""
    manufacturer: str = ""
    warranty: str = ""
    package_contents: str = ""
    source_price: str | None = None
    purchase_price: str | None = None
    selling_price: str | None = None
    economics_reference: dict[str, Any] = field(default_factory=dict)
    completeness: str = INSUFFICIENT_INPUT
    missing_required: tuple[str, ...] = ()
    missing_recommended: tuple[str, ...] = ()
    unknown_facts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    field_provenance: dict[str, str] = field(default_factory=dict)
    version: str = ""
    content_status: str = PARTIAL

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["attributes"] = [a.as_dict() if hasattr(a, "as_dict") else a for a in self.attributes]
        d["specifications"] = {
            k: v.as_dict() if hasattr(v, "as_dict") else v for k, v in self.specifications.items()
        }
        return d


@dataclass(frozen=True)
class SeoPackage:
    seo_title: str
    meta_description: str
    canonical_slug: str
    heading: str
    product_summary: str
    keyword_candidates: tuple[str, ...]
    keyword_note: str
    structured_sections: tuple[str, ...]
    internal_link_hints: tuple[str, ...]
    schema_product: dict[str, Any]
    schema_readiness: bool
    quality: dict[str, Any]
    warnings: tuple[str, ...]
    issues: tuple[str, ...]
    field_provenance: dict[str, str]
    duplicate_title: bool = False
    duplicate_slug: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaAssetRecord:
    asset_id: str
    product_id: str
    source: str
    source_type: str
    mime_type: str
    width: int
    height: int
    file_size: int
    checksum: str
    role: str
    sort_order: int
    alt_text: str
    caption: str
    provenance: str
    validation_status: str
    warnings: tuple[str, ...]
    kind: str = SOURCE_IMAGE
    original_preserved: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaPackage:
    assets: tuple[MediaAssetRecord, ...]
    checksums: tuple[str, ...]
    duplicate_checksums: tuple[str, ...]
    original_bytes_by_asset: dict[str, bytes] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "assets": [a.as_dict() for a in self.assets],
            "checksums": list(self.checksums),
            "duplicate_checksums": list(self.duplicate_checksums),
            "original_bytes_by_asset": {k: f"bytes:{len(v)}" for k, v in self.original_bytes_by_asset.items()},
            "warnings": list(self.warnings),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class ProductContentPackage:
    tenant_id: str
    package_id: str
    product_id: str
    version: str
    status: str
    card: ProductCard
    seo: SeoPackage
    media: MediaPackage
    validation: dict[str, Any]
    provenance: dict[str, Any]
    warnings: tuple[str, ...]
    issues: tuple[str, ...]
    published: bool = False
    policy_version: str = POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "package_id": self.package_id,
            "product_id": self.product_id,
            "version": self.version,
            "status": self.status,
            "card": self.card.as_dict(),
            "seo": self.seo.as_dict(),
            "media": self.media.as_dict(),
            "validation": dict(self.validation),
            "provenance": dict(self.provenance),
            "warnings": list(self.warnings),
            "issues": list(self.issues),
            "published": self.published,
            "policy_version": self.policy_version,
        }


def content_version(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
