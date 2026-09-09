"""Canonical enrichment data model (product_enrichment package docstring).

Generic by construction -- no TV-only (or any other category-only) fields.
Every characteristic is a free-form ``canonical_key -> NormalizedCharacteristic``
entry; category-specific behavior lives only in the alias/unit tables in
``characteristics.py``, never in this module's shape.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Mapping

# --- Source trust taxonomy (requirement 2: source priority) ---------------

SOURCE_MANUFACTURER = "manufacturer"
SOURCE_MANUFACTURER_DOCUMENTATION = "manufacturer_documentation"
SOURCE_AUTHORIZED_DISTRIBUTOR = "authorized_distributor"
SOURCE_RETAIL_CATALOG = "retail_catalog"
SOURCE_UNKNOWN = "unknown"

# Higher rank == more trusted. Mirrors the required priority order exactly
# (manufacturer / manufacturer docs > authorized distributor > reputable
# retail as secondary corroboration only).
SOURCE_TRUST_RANK: Mapping[str, int] = {
    SOURCE_MANUFACTURER: 4,
    SOURCE_MANUFACTURER_DOCUMENTATION: 4,
    SOURCE_AUTHORIZED_DISTRIBUTOR: 3,
    SOURCE_RETAIL_CATALOG: 2,
    SOURCE_UNKNOWN: 0,
}

# --- Confidence / verification state ---------------------------------------

CONFIDENCE_VERIFIED = "verified"
CONFIDENCE_PROBABLE = "probable"
CONFIDENCE_UNVERIFIED = "unverified"
CONFIDENCE_CONFLICTING = "conflicting"


class IdentityConflictError(Exception):
    """Fail-closed identity resolution error (requirement 1). ``code`` is a
    stable machine-readable reason; never silently guessed/merged."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class ProductIdentityQuery:
    """Raw, unverified identity inputs -- exactly what a supplier XLSX row
    already carries (brand/model/article/EAN/category/subcategory)."""

    brand: str
    model: str = ""
    article: str = ""
    ean: str = ""
    category: str = ""
    subcategory: str = ""


def compute_identity_key(*, brand: str, model: str, ean: str) -> str:
    """Deterministic cache/dedup key (requirement 13: EAN + brand + exact
    model). Stable across process restarts -- never random."""
    basis = f"{brand.strip().casefold()}|{model.strip().casefold()}|{ean.strip()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ResolvedIdentity:
    """Fail-closed-resolved product identity (requirement 1)."""

    brand: str
    model: str
    article: str
    ean: str
    category: str
    subcategory: str
    identity_key: str
    # "ean_and_model" (strongest) | "model_only" | "article_only"
    strength: str = "model_only"


@dataclass(frozen=True)
class SourceFact:
    """One provenance-tracked fact discovered during research (requirement
    2): source URL/type, raw + normalized value, unit, confidence,
    retrieval timestamp. ``characteristic_key`` is "" for identity/content-
    level facts (e.g. a confirmed exact model name) rather than a
    characteristic."""

    characteristic_key: str
    raw_label: str
    raw_value: str
    normalized_value: str
    unit: str
    source_url: str
    source_type: str
    source_domain: str
    confidence: str
    retrieved_at: str = ""


@dataclass(frozen=True)
class NormalizedCharacteristic:
    """A characteristic in Panda's generic canonical shape (requirement 3):
    canonical name + normalized value + optional unit + provenance +
    confidence, plus (if resolvable) the verified Bitrix property it maps
    to -- never a guessed one."""

    key: str
    value: str
    unit: str = ""
    confidence: str = CONFIDENCE_UNVERIFIED
    supporting_facts: tuple[SourceFact, ...] = field(default_factory=tuple)
    bitrix_property_id: int | None = None
    bitrix_writable: bool = False


@dataclass(frozen=True)
class ContentDraft:
    """Fact-only generated content (requirement 4). Every phrase in
    ``short_description``/``detailed_description`` is built exclusively
    from ``ResolvedIdentity`` + accepted ``NormalizedCharacteristic``
    values -- see ``content.py`` -- never invented."""

    short_description: str = ""
    detailed_description: str = ""
    seo_title: str = ""
    seo_description: str = ""
    seo_keywords: tuple[str, ...] = ()
    image_alt: str = ""
    image_title: str = ""
    facts_used: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaCandidateInput:
    """One candidate product-image URL to acquire, with its provenance
    source type -- ordering in the caller-supplied sequence encodes source
    preference (requirement 5: manufacturer first)."""

    url: str
    source_type: str = SOURCE_UNKNOWN


@dataclass(frozen=True)
class RejectedMediaCandidate:
    source_url: str
    source_type: str
    reason: str


@dataclass(frozen=True)
class MediaAsset:
    """One processed, Bitrix-ready media derivative (requirement 6/8): the
    ONLY thing ever handed to the Bitrix write path -- never the original
    external URL (see ``media.py``)."""

    role: str  # "preview" | "detail" | "gallery"
    content_hash: str
    filename: str
    base64_content: str
    width: int
    height: int
    mime_type: str
    source_url: str
    source_type: str
    generated: bool = False
    processing: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaResult:
    STATUS_READY = "ready"
    STATUS_PARTIAL = "partial"
    STATUS_UNRESOLVED = "unresolved"

    assets: tuple[MediaAsset, ...] = ()
    rejected_candidates: tuple[RejectedMediaCandidate, ...] = ()
    status: str = "unresolved"


@dataclass(frozen=True)
class IdentityConflict:
    code: str
    detail: str
    conflicting_facts: tuple[SourceFact, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EnrichmentResult:
    """The complete enrichment output for one product (requirement 11:
    "preview must become complete"). Handed to
    ``business_assistant.product_enrichment_bridge`` to build the final,
    existing ``SingleProductWriteRequest`` -- this module never talks to
    Bitrix directly."""

    identity: ResolvedIdentity
    characteristics: Mapping[str, NormalizedCharacteristic] = field(default_factory=dict)
    content: ContentDraft = field(default_factory=ContentDraft)
    media: MediaResult = field(default_factory=MediaResult)
    facts: tuple[SourceFact, ...] = field(default_factory=tuple)
    conflicts: tuple[IdentityConflict, ...] = field(default_factory=tuple)
    research_available: bool = True
    cache_hit: bool = False
