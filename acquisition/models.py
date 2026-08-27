"""Canonical acquisition contracts — immutable, no credentials."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id

# Source types
SOURCE_SUPPLIER = "supplier"
SOURCE_MANUFACTURER = "manufacturer"
SOURCE_COMPETITOR = "competitor"
SOURCE_MARKETPLACE = "marketplace"
SOURCE_SEARCH = "search"
SOURCE_WEBSITE = "website"
SOURCE_API = "api"
SOURCE_DOCUMENT = "document"
SOURCE_FEED = "feed"
SOURCE_TYPES = (
    SOURCE_SUPPLIER,
    SOURCE_MANUFACTURER,
    SOURCE_COMPETITOR,
    SOURCE_MARKETPLACE,
    SOURCE_SEARCH,
    SOURCE_WEBSITE,
    SOURCE_API,
    SOURCE_DOCUMENT,
    SOURCE_FEED,
)

# Source trust (evidence quality — not Tool trust)
TRUST_OFFICIAL_MANUFACTURER = "official_manufacturer"
TRUST_CONTRACTED_SUPPLIER = "contracted_supplier"
TRUST_MARKETPLACE_API = "marketplace_api"
TRUST_KNOWN_RETAILER = "known_retailer"
TRUST_GENERAL_WEB = "general_web"
TRUST_UNKNOWN = "unknown"
SOURCE_TRUST_LEVELS = (
    TRUST_OFFICIAL_MANUFACTURER,
    TRUST_CONTRACTED_SUPPLIER,
    TRUST_MARKETPLACE_API,
    TRUST_KNOWN_RETAILER,
    TRUST_GENERAL_WEB,
    TRUST_UNKNOWN,
)

# Acquisition types
ACQ_HTTP_GET = "http_get"
ACQ_SEARCH = "search"
ACQ_DOCUMENT = "document"
ACQ_CRAWL = "crawl"
ACQ_BROWSER = "browser"
ACQ_FEED = "feed"
ACQUISITION_TYPES = (
    ACQ_HTTP_GET,
    ACQ_SEARCH,
    ACQ_DOCUMENT,
    ACQ_CRAWL,
    ACQ_BROWSER,
    ACQ_FEED,
)

# Record types
RECORD_PRICE = "price"
RECORD_SUPPLIER_ITEM = "supplier_item"
RECORD_COMPETITOR = "competitor"
RECORD_MARKETPLACE = "marketplace"
RECORD_SEARCH_HIT = "search_hit"
RECORD_DOCUMENT = "document"
RECORD_GENERIC = "generic"
RECORD_TYPES = (
    RECORD_PRICE,
    RECORD_SUPPLIER_ITEM,
    RECORD_COMPETITOR,
    RECORD_MARKETPLACE,
    RECORD_SEARCH_HIT,
    RECORD_DOCUMENT,
    RECORD_GENERIC,
)

# Freshness labels
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"

# Match confidence
MATCH_EXACT = "exact"
MATCH_HIGH = "high"
MATCH_MEDIUM = "medium"
MATCH_LOW = "low"
MATCH_UNRESOLVED = "unresolved"
MATCH_LEVELS = (MATCH_EXACT, MATCH_HIGH, MATCH_MEDIUM, MATCH_LOW, MATCH_UNRESOLVED)

# Change outcomes
CHANGE_CREATED = "created"
CHANGE_UNCHANGED = "unchanged"
CHANGE_CHANGED = "changed"
CHANGE_REMOVED = "removed"
CHANGE_OUTCOMES = (CHANGE_CREATED, CHANGE_UNCHANGED, CHANGE_CHANGED, CHANGE_REMOVED)

# Content marked untrusted for LLM
CONTENT_TRUST_UNTRUSTED = "untrusted_external"
CONTENT_TRUST_INTERNAL = "internal"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def checksum_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum_text(text: str) -> str:
    return checksum_bytes((text or "").encode("utf-8"))


def fingerprint_record(fields: Mapping[str, object]) -> str:
    payload = json.dumps(
        sanitize_metadata(dict(fields or {})),
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_id(prefix: str = "") -> str:
    raw = str(uuid.uuid4())
    return f"{prefix}{raw}" if prefix else raw


@dataclass(frozen=True)
class FreshnessPolicy:
    stale_after_seconds: int | None = 86400
    unknown_if_missing_timestamp: bool = True

    def __post_init__(self):
        if self.stale_after_seconds is not None and int(self.stale_after_seconds) < 0:
            raise ValueError("stale_after_seconds_invalid")


@dataclass(frozen=True)
class SourceDescriptor:
    source_id: str
    source_type: str
    tenant_id: str
    trust_level: str
    freshness_policy: FreshnessPolicy = field(default_factory=FreshnessPolicy)
    tool_id: str = ""
    integration_id: str = ""
    enabled: bool = True
    name: str = ""
    allowed_domains: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not str(self.source_id or "").strip():
            raise ValueError("source_id_required")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"invalid_source_type:{self.source_type}")
        if self.trust_level not in SOURCE_TRUST_LEVELS:
            raise ValueError(f"invalid_trust_level:{self.trust_level}")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "allowed_domains", tuple(self.allowed_domains or ()))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        # Reject credential-like keys
        for key in self.metadata:
            lowered = str(key).lower()
            if any(s in lowered for s in ("secret", "password", "token", "api_key", "credential")):
                raise ValueError("credentials_forbidden_in_source_metadata")


@dataclass(frozen=True)
class AcquisitionRequest:
    source_id: str
    target: str
    acquisition_type: str
    tenant_id: str
    workflow_id: str = ""
    request_id: str = field(default_factory=lambda: new_id("acq-"))
    requested_at: datetime = field(default_factory=utc_now)
    constraints: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.acquisition_type not in ACQUISITION_TYPES:
            raise ValueError(f"invalid_acquisition_type:{self.acquisition_type}")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "constraints", _meta(self.constraints))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class RawArtifact:
    artifact_id: str
    source_id: str
    tenant_id: str
    content_type: str
    fetched_at: datetime
    checksum: str
    content_ref: str = ""
    content_text: str = ""
    content_bytes_len: int = 0
    document_id: str = ""
    url: str = ""
    content_trust: str = CONTENT_TRUST_UNTRUSTED
    provenance: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        for key in list(self.metadata) + list(self.provenance):
            lowered = str(key).lower()
            if any(s in lowered for s in ("secret", "password", "token", "api_key", "authorization")):
                raise ValueError("credentials_forbidden_in_artifact")


@dataclass(frozen=True)
class ParsedRecord:
    record_id: str
    parser_id: str
    parser_version: str
    source_id: str
    artifact_id: str
    tenant_id: str
    record_type: str
    fields: Mapping[str, object]
    confidence: float
    fingerprint: str
    observed_at: datetime
    provenance: Mapping[str, object] = field(default_factory=dict)
    raw_field_refs: Mapping[str, object] = field(default_factory=dict)
    validation_ok: bool = True
    validation_errors: tuple[str, ...] = ()
    freshness: str = FRESHNESS_UNKNOWN
    content_trust: str = CONTENT_TRUST_UNTRUSTED
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.record_type not in RECORD_TYPES:
            raise ValueError(f"invalid_record_type:{self.record_type}")
        score = float(self.confidence)
        if score < 0.0:
            score = 0.0
        if score > 1.0:
            score = 1.0
        object.__setattr__(self, "confidence", score)
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "fields", _meta(self.fields))
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "raw_field_refs", _meta(self.raw_field_refs))
        object.__setattr__(self, "validation_errors", tuple(self.validation_errors or ()))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class MatchEvidence:
    method: str
    matched_fields: tuple[str, ...]
    conflicts: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "matched_fields", tuple(self.matched_fields or ()))
        object.__setattr__(self, "conflicts", tuple(self.conflicts or ()))
        object.__setattr__(self, "details", _meta(self.details))


@dataclass(frozen=True)
class EntityMatchResult:
    left_record_id: str
    right_record_id: str
    level: str
    confidence: float
    evidence: MatchEvidence
    same_entity: bool

    def __post_init__(self):
        if self.level not in MATCH_LEVELS:
            raise ValueError(f"invalid_match_level:{self.level}")
        score = float(self.confidence)
        if score < 0.0:
            score = 0.0
        if score > 1.0:
            score = 1.0
        object.__setattr__(self, "confidence", score)


@dataclass(frozen=True)
class ChangeEvent:
    change_id: str
    tenant_id: str
    source_id: str
    record_id: str
    outcome: str
    previous_fingerprint: str | None
    new_fingerprint: str | None
    changed_fields: tuple[str, ...] = ()
    observed_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.outcome not in CHANGE_OUTCOMES:
            raise ValueError(f"invalid_change_outcome:{self.outcome}")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "changed_fields", tuple(self.changed_fields or ()))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "errors", tuple(self.errors or ()))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))
