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


# ---------------------------------------------------------------------------
# Scale platform (5.1–5.7) — versioned job / resource / ingest contracts
# ---------------------------------------------------------------------------

# Job modes
MODE_SINGLE = "single"
MODE_CRAWL = "crawl"
MODE_SCRAPE = "scrape"
MODE_API = "api"
ACQUISITION_MODES = (MODE_SINGLE, MODE_CRAWL, MODE_SCRAPE, MODE_API)

# Job status
JOB_PENDING = "pending"
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_PARTIAL = "partial"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_REJECTED = "rejected"
JOB_STATUSES = (
    JOB_PENDING,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_COMPLETED,
    JOB_PARTIAL,
    JOB_FAILED,
    JOB_CANCELLED,
    JOB_REJECTED,
)

# Resource status
RESOURCE_PENDING = "pending"
RESOURCE_FETCHED = "fetched"
RESOURCE_PARSED = "parsed"
RESOURCE_SKIPPED = "skipped"
RESOURCE_FAILED = "failed"
RESOURCE_DENIED = "denied"
RESOURCE_STATUSES = (
    RESOURCE_PENDING,
    RESOURCE_FETCHED,
    RESOURCE_PARSED,
    RESOURCE_SKIPPED,
    RESOURCE_FAILED,
    RESOURCE_DENIED,
)

# Extraction / field status (never invent values)
EXTRACT_OK = "ok"
EXTRACT_MISSING = "missing"
EXTRACT_INVALID = "invalid"
EXTRACT_EMPTY = "empty"
EXTRACT_UNAVAILABLE = "unavailable"
EXTRACTION_STATUSES = (
    EXTRACT_OK,
    EXTRACT_MISSING,
    EXTRACT_INVALID,
    EXTRACT_EMPTY,
    EXTRACT_UNAVAILABLE,
)

# Frontier states
FRONTIER_PENDING = "pending"
FRONTIER_CLAIMED = "claimed"
FRONTIER_COMPLETED = "completed"
FRONTIER_SKIPPED = "skipped"
FRONTIER_FAILED = "failed"
FRONTIER_RETRY = "retry"
FRONTIER_STATUSES = (
    FRONTIER_PENDING,
    FRONTIER_CLAIMED,
    FRONTIER_COMPLETED,
    FRONTIER_SKIPPED,
    FRONTIER_FAILED,
    FRONTIER_RETRY,
)

# Dedupe decisions
DEDUPE_UNIQUE = "unique"
DEDUPE_EXACT = "exact"
DEDUPE_SAME_SOURCE = "same_source"
DEDUPE_CROSS_SOURCE = "cross_source"
DEDUPE_POSSIBLE = "possible"
DEDUPE_DECISIONS = (
    DEDUPE_UNIQUE,
    DEDUPE_EXACT,
    DEDUPE_SAME_SOURCE,
    DEDUPE_CROSS_SOURCE,
    DEDUPE_POSSIBLE,
)

# Ingest outcomes
INGEST_ACCEPTED = "accepted"
INGEST_REJECTED = "rejected"
INGEST_DUPLICATE = "duplicate"
INGEST_FAILED = "failed"
INGEST_OUTCOMES = (INGEST_ACCEPTED, INGEST_REJECTED, INGEST_DUPLICATE, INGEST_FAILED)

POLICY_VERSION = "1.0.0"
PARSER_CONTRACT_VERSION = "1.0.0"
NORMALIZER_VERSION = "1.0.0"
DEDUPE_POLICY_VERSION = "1.0.0"
INGESTION_VERSION = "1.0.0"


@dataclass(frozen=True)
class CrawlPolicy:
    """Trusted crawl limits — not overridable from untrusted payload."""

    max_depth: int = 2
    max_pages: int = 50
    max_frontier: int = 500
    per_host_concurrency: int = 2
    min_interval_seconds: float = 0.0
    max_redirects: int = 5
    ignore_tracking_params: bool = True
    allowed_content_types: tuple[str, ...] = (
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "text/plain",
        "text/csv",
    )
    path_allow: tuple[str, ...] = ()
    path_deny: tuple[str, ...] = ()
    respect_robots: bool = True
    deadline_seconds: float | None = None
    # Retries after the initial fetch attempt. Max fetch attempts = 1 + max_retries_per_url.
    max_retries_per_url: int = 3

    def __post_init__(self):
        object.__setattr__(self, "allowed_content_types", tuple(self.allowed_content_types or ()))
        object.__setattr__(self, "path_allow", tuple(self.path_allow or ()))
        object.__setattr__(self, "path_deny", tuple(self.path_deny or ()))
        if int(self.max_depth) < 0:
            raise ValueError("max_depth_invalid")
        if int(self.max_pages) < 1:
            raise ValueError("max_pages_invalid")
        if int(self.max_retries_per_url) < 0:
            raise ValueError("max_retries_per_url_invalid")
        object.__setattr__(self, "max_retries_per_url", int(self.max_retries_per_url))


@dataclass(frozen=True)
class SourceDefinition:
    """Versioned source contract — host allowlist is trusted control-plane only."""

    source_id: str
    source_type: str
    tenant_id: str
    trust_level: str
    allowed_hosts: tuple[str, ...]
    seed_urls: tuple[str, ...] = ()
    path_allow: tuple[str, ...] = ()
    path_deny: tuple[str, ...] = ()
    auth_secret_ref: str = ""
    crawl_policy: CrawlPolicy = field(default_factory=CrawlPolicy)
    freshness_policy: FreshnessPolicy = field(default_factory=FreshnessPolicy)
    tool_id: str = "http.request"
    integration_id: str = ""
    enabled: bool = True
    name: str = ""
    policy_version: str = POLICY_VERSION
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        from security.tenant import require_tenant_id

        if not str(self.source_id or "").strip():
            raise ValueError("source_id_required")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"invalid_source_type:{self.source_type}")
        if self.trust_level not in SOURCE_TRUST_LEVELS:
            raise ValueError(f"invalid_trust_level:{self.trust_level}")
        hosts = tuple(h.strip().lower() for h in (self.allowed_hosts or ()) if str(h).strip())
        if not hosts:
            raise ValueError("allowed_hosts_required")
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "seed_urls", tuple(self.seed_urls or ()))
        object.__setattr__(self, "path_allow", tuple(self.path_allow or ()))
        object.__setattr__(self, "path_deny", tuple(self.path_deny or ()))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        if self.auth_secret_ref:
            from tools.secrets_ref import ensure_secret_ref

            ref = ensure_secret_ref(self.auth_secret_ref)
            object.__setattr__(self, "auth_secret_ref", ref.secret_ref if ref else "")
        for key in self.metadata:
            lowered = str(key).lower()
            if any(s in lowered for s in ("secret", "password", "token", "api_key", "credential")):
                raise ValueError("credentials_forbidden_in_source_metadata")

    def to_descriptor(self) -> SourceDescriptor:
        return SourceDescriptor(
            source_id=self.source_id,
            source_type=self.source_type,
            tenant_id=self.tenant_id,
            trust_level=self.trust_level,
            freshness_policy=self.freshness_policy,
            tool_id=self.tool_id,
            integration_id=self.integration_id,
            enabled=self.enabled,
            name=self.name,
            allowed_domains=self.allowed_hosts,
            metadata={
                **dict(self.metadata),
                "policy_version": self.policy_version,
                "auth_ref": self.auth_secret_ref,
                "seed_urls": list(self.seed_urls),
                "path_allow": list(self.path_allow),
                "path_deny": list(self.path_deny),
            },
        )

    @classmethod
    def from_descriptor(cls, descriptor: SourceDescriptor, **overrides) -> "SourceDefinition":
        meta = dict(descriptor.metadata or {})
        hosts = tuple(descriptor.allowed_domains or ())
        if not hosts and overrides.get("allowed_hosts"):
            hosts = tuple(overrides["allowed_hosts"])
        return cls(
            source_id=overrides.get("source_id", descriptor.source_id),
            source_type=overrides.get("source_type", descriptor.source_type),
            tenant_id=overrides.get("tenant_id", descriptor.tenant_id),
            trust_level=overrides.get("trust_level", descriptor.trust_level),
            allowed_hosts=hosts,
            seed_urls=tuple(overrides.get("seed_urls", meta.get("seed_urls") or ())),
            path_allow=tuple(overrides.get("path_allow", meta.get("path_allow") or ())),
            path_deny=tuple(overrides.get("path_deny", meta.get("path_deny") or ())),
            auth_secret_ref=str(overrides.get("auth_secret_ref", meta.get("auth_ref") or meta.get("auth_secret_ref") or "")),
            crawl_policy=overrides.get("crawl_policy", CrawlPolicy()),
            freshness_policy=overrides.get("freshness_policy", descriptor.freshness_policy),
            tool_id=overrides.get("tool_id", descriptor.tool_id or "http.request"),
            integration_id=overrides.get("integration_id", descriptor.integration_id),
            enabled=overrides.get("enabled", descriptor.enabled),
            name=overrides.get("name", descriptor.name),
            policy_version=str(overrides.get("policy_version", meta.get("policy_version") or POLICY_VERSION)),
            metadata={k: v for k, v in meta.items() if k not in {
                "policy_version", "auth_secret_ref", "auth_ref", "seed_urls", "path_allow", "path_deny",
            }},
        )


@dataclass(frozen=True)
class AcquisitionJob:
    job_id: str
    tenant_id: str
    actor_id: str
    source_id: str
    mode: str
    workload_class: str
    status: str = JOB_PENDING
    workflow_id: str = ""
    trusted_job_type: str = ""
    execution_lane: str = ""
    policy_version: str = POLICY_VERSION
    parser_version: str = PARSER_CONTRACT_VERSION
    normalizer_version: str = NORMALIZER_VERSION
    dedupe_version: str = DEDUPE_POLICY_VERSION
    ingestion_version: str = INGESTION_VERSION
    scrape_profile_id: str = ""
    scrape_profile_version: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancel_requested: bool = False
    error_code: str = ""
    counters: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        from security.tenant import require_tenant_id

        if self.mode not in ACQUISITION_MODES:
            raise ValueError(f"invalid_mode:{self.mode}")
        if self.status not in JOB_STATUSES:
            raise ValueError(f"invalid_job_status:{self.status}")
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "counters", _meta(self.counters))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class AcquiredResource:
    resource_id: str
    job_id: str
    tenant_id: str
    source_id: str
    url: str
    status: str = RESOURCE_PENDING
    content_type: str = ""
    content_length: int = 0
    content_hash: str = ""
    raw_artifact_ref: str = ""
    canonical_url: str = ""
    depth: int = 0
    parent_url: str = ""
    extraction_status: str = EXTRACT_OK
    provenance: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        from security.tenant import require_tenant_id

        if self.status not in RESOURCE_STATUSES:
            raise ValueError(f"invalid_resource_status:{self.status}")
        if self.extraction_status not in EXTRACTION_STATUSES:
            raise ValueError(f"invalid_extraction_status:{self.extraction_status}")
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class NormalizedRecord:
    record_id: str
    job_id: str
    tenant_id: str
    source_id: str
    resource_id: str
    normalizer_version: str
    fields: Mapping[str, object]
    field_status: Mapping[str, object]
    fingerprint: str
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    provenance: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        from security.tenant import require_tenant_id

        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "fields", _meta(self.fields))
        object.__setattr__(self, "field_status", _meta(self.field_status))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))
        object.__setattr__(self, "errors", tuple(self.errors or ()))
        object.__setattr__(self, "provenance", _meta(self.provenance))


@dataclass(frozen=True)
class DedupeDecision:
    decision_id: str
    tenant_id: str
    job_id: str
    record_id: str
    decision: str
    matched_record_id: str = ""
    layer: str = ""
    policy_version: str = DEDUPE_POLICY_VERSION
    provenance_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        from security.tenant import require_tenant_id

        if self.decision not in DEDUPE_DECISIONS:
            raise ValueError(f"invalid_dedupe_decision:{self.decision}")
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "provenance_refs", tuple(self.provenance_refs or ()))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class DatasetResult:
    dataset_id: str
    tenant_id: str
    job_id: str
    name: str
    version: str
    record_count: int
    fingerprint: str
    source_ids: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        from security.tenant import require_tenant_id

        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "source_ids", tuple(self.source_ids or ()))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class IngestionBatchResult:
    batch_id: str
    tenant_id: str
    job_id: str
    dataset_id: str
    accepted: int = 0
    rejected: int = 0
    duplicate: int = 0
    failed: int = 0
    reason_codes: tuple[str, ...] = ()
    idempotency_key: str = ""
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        from security.tenant import require_tenant_id

        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes or ()))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class FrontierEntry:
    entry_id: str
    job_id: str
    tenant_id: str
    url: str
    canonical_url: str
    status: str = FRONTIER_PENDING
    depth: int = 0
    parent_url: str = ""
    retry_count: int = 0
    claim_token: str = ""
    error_code: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        from security.tenant import require_tenant_id

        if self.status not in FRONTIER_STATUSES:
            raise ValueError(f"invalid_frontier_status:{self.status}")
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class CrawlCheckpoint:
    job_id: str
    tenant_id: str
    visited_count: int = 0
    frontier_pending: int = 0
    pages_fetched: int = 0
    pages_failed: int = 0
    pages_skipped: int = 0
    policy_version: str = POLICY_VERSION
    parser_version: str = PARSER_CONTRACT_VERSION
    normalizer_version: str = NORMALIZER_VERSION
    dedupe_version: str = DEDUPE_POLICY_VERSION
    updated_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        from security.tenant import require_tenant_id

        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "metadata", _meta(self.metadata))
