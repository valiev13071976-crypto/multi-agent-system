"""Block 6 platform domain models — immutable, tenant fail-closed."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id, require_tenant_id

PLATFORM_SCHEMA_VERSION = "1.0.0"
CLASSIFIER_VERSION = "v1"
RECONCILIATION_PROFILE_VERSION = "1.0.0"
COMPARISON_PROFILE_VERSION = "1.0.0"

# Field extraction statuses
FIELD_FOUND = "FOUND"
FIELD_MISSING = "MISSING"
FIELD_INVALID = "INVALID"
FIELD_AMBIGUOUS = "AMBIGUOUS"
FIELD_LOW_CONFIDENCE = "LOW_CONFIDENCE"
FIELD_UNAVAILABLE = "UNAVAILABLE"
FIELD_EXTRACTION_STATUSES = (
    FIELD_FOUND,
    FIELD_MISSING,
    FIELD_INVALID,
    FIELD_AMBIGUOUS,
    FIELD_LOW_CONFIDENCE,
    FIELD_UNAVAILABLE,
)

# Classification
DOC_CLASS_UNKNOWN = "unknown"
CLASS_STATUS_OK = "ok"
CLASS_STATUS_UNKNOWN = "unknown"
CLASS_STATUS_FAILED = "failed"

# OCR plan statuses
OCR_NOT_REQUIRED = "not_required"
OCR_REQUIRED = "required"
OCR_PERFORMED = "performed"
OCR_PARTIAL = "partial"
OCR_UNAVAILABLE = "unavailable"
OCR_FAILED = "failed"
OCR_PLAN_STATUSES = (
    OCR_NOT_REQUIRED,
    OCR_REQUIRED,
    OCR_PERFORMED,
    OCR_PARTIAL,
    OCR_UNAVAILABLE,
    OCR_FAILED,
)

# Reconciliation
RECON_MATCH = "MATCH"
RECON_MISMATCH = "MISMATCH"
RECON_PARTIAL = "PARTIAL"
RECON_AMBIGUOUS = "AMBIGUOUS"
RECON_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
RECONCILIATION_STATUSES = (
    RECON_MATCH,
    RECON_MISMATCH,
    RECON_PARTIAL,
    RECON_AMBIGUOUS,
    RECON_INSUFFICIENT_DATA,
)

# Job statuses / stages
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

STAGE_INGEST = "ingest"
STAGE_OCR = "ocr"
STAGE_CLASSIFY = "classify"
STAGE_EXTRACT = "extract"
STAGE_VALIDATE = "validate"
STAGE_COMPARE = "compare"
STAGE_RECONCILE = "reconcile"
STAGE_GENERATE = "generate"
STAGE_DONE = "done"

OP_INGEST = "ingest"
OP_OCR = "ocr"
OP_CLASSIFY = "classify"
OP_EXTRACT = "extract"
OP_COMPARE = "compare"
OP_RECONCILE = "reconcile"
OP_GENERATE = "generate"
DOCUMENT_OPERATIONS = (
    OP_INGEST,
    OP_OCR,
    OP_CLASSIFY,
    OP_EXTRACT,
    OP_COMPARE,
    OP_RECONCILE,
    OP_GENERATE,
)

TRUSTED_JOB_DOCUMENT_OCR = "document_ocr"
TRUSTED_JOB_DOCUMENT_LARGE = "document_large"
TRUSTED_JOB_DOCUMENT_BULK = "document_bulk"
DOCUMENT_TRUSTED_JOB_TYPES = frozenset(
    {TRUSTED_JOB_DOCUMENT_OCR, TRUSTED_JOB_DOCUMENT_LARGE, TRUSTED_JOB_DOCUMENT_BULK}
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4()}"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


@dataclass(frozen=True)
class DocumentRef:
    document_id: str
    artifact_id: str
    tenant_id: str
    owner_id: str = ""
    execution_id: str = ""
    workflow_id: str = ""
    task_id: str = ""
    source: str = ""
    filename: str = ""
    media_type: str = ""
    byte_size: int = 0
    content_hash: str = ""
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "byte_size", int(self.byte_size or 0))


@dataclass(frozen=True)
class DocumentVersion:
    document_id: str
    version_id: str
    artifact_id: str
    content_hash: str
    parent_version_id: str = ""
    transformation_reason: str = ""
    producing_operation: str = ""
    producing_tool_or_model: str = ""
    provenance: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "provenance", _meta(self.provenance))


@dataclass(frozen=True)
class DocumentProcessingJob:
    job_id: str
    document_id: str
    tenant_id: str
    version_id: str = ""
    execution_id: str = ""
    workflow_id: str = ""
    task_id: str = ""
    operations: tuple[str, ...] = ()
    workload_class: str = "normal"
    execution_lane: str = "default"
    profile_version: str = PLATFORM_SCHEMA_VERSION
    status: str = JOB_PENDING
    stage: str = STAGE_INGEST
    checkpoint: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    idempotency_key: str = ""
    pinned_providers: Mapping[str, object] = field(default_factory=dict)
    pinned_profiles: Mapping[str, object] = field(default_factory=dict)
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "operations", tuple(self.operations or ()))
        object.__setattr__(self, "checkpoint", _meta(self.checkpoint))
        object.__setattr__(self, "pinned_providers", _meta(self.pinned_providers))
        object.__setattr__(self, "pinned_profiles", _meta(self.pinned_profiles))


@dataclass(frozen=True)
class DocumentResult:
    document_id: str
    version_id: str
    status: str
    text_ref: str = ""
    blocks: tuple[Mapping[str, object], ...] = ()
    tables: tuple[Mapping[str, object], ...] = ()
    classification: Mapping[str, object] = field(default_factory=dict)
    fields: Mapping[str, object] = field(default_factory=dict)
    validation: Mapping[str, object] = field(default_factory=dict)
    provenance: Mapping[str, object] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    generated_artifact_ids: tuple[str, ...] = ()
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "blocks", tuple(dict(b) for b in (self.blocks or ())))
        object.__setattr__(self, "tables", tuple(dict(t) for t in (self.tables or ())))
        object.__setattr__(self, "classification", _meta(self.classification))
        object.__setattr__(self, "fields", _meta(self.fields))
        object.__setattr__(self, "validation", _meta(self.validation))
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))
        object.__setattr__(self, "errors", tuple(self.errors or ()))
        object.__setattr__(
            self, "generated_artifact_ids", tuple(self.generated_artifact_ids or ())
        )


# Alias for taxonomy naming in specs
FieldExtractionStatus = type(
    "FieldExtractionStatus",
    (),
    {
        "FOUND": FIELD_FOUND,
        "MISSING": FIELD_MISSING,
        "INVALID": FIELD_INVALID,
        "AMBIGUOUS": FIELD_AMBIGUOUS,
        "LOW_CONFIDENCE": FIELD_LOW_CONFIDENCE,
        "UNAVAILABLE": FIELD_UNAVAILABLE,
    },
)


@dataclass(frozen=True)
class ExtractionFieldSpec:
    name: str
    type: str = "string"
    required: bool = False
    aliases: tuple[str, ...] = ()
    validation: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "aliases", tuple(self.aliases or ()))
        object.__setattr__(self, "validation", _meta(self.validation))


@dataclass(frozen=True)
class ExtractionSchema:
    schema_id: str
    fields: tuple[ExtractionFieldSpec, ...]
    version: str = "1.0.0"
    document_type: str = ""

    def __post_init__(self):
        object.__setattr__(self, "fields", tuple(self.fields or ()))


@dataclass(frozen=True)
class FieldValue:
    name: str
    status: str
    value: object = None
    confidence: str = "medium"
    method: str = "rule"
    evidence: str = ""
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in FIELD_EXTRACTION_STATUSES:
            object.__setattr__(self, "status", FIELD_INVALID)
        object.__setattr__(self, "provenance", _meta(self.provenance))


@dataclass(frozen=True)
class ClassificationResult:
    doc_class: str
    classifier_version: str = CLASSIFIER_VERSION
    confidence: str = "low"
    evidence: tuple[str, ...] = ()
    alternatives: tuple[Mapping[str, object], ...] = ()
    status: str = CLASS_STATUS_OK
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(self.evidence or ()))
        object.__setattr__(
            self, "alternatives", tuple(dict(a) for a in (self.alternatives or ()))
        )
        if self.doc_class in {"", DOC_CLASS_UNKNOWN, "generic_document"} and self.status == CLASS_STATUS_OK:
            # generic with no strong evidence is treated as unknown by classify helpers
            pass


@dataclass(frozen=True)
class OCRPlanDecision:
    status: str
    reason: str = ""
    page_count: int = 0
    provider: str = ""
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        if self.status not in OCR_PLAN_STATUSES:
            object.__setattr__(self, "status", OCR_REQUIRED)
        object.__setattr__(self, "page_count", int(self.page_count or 0))


@dataclass(frozen=True)
class ComparisonResult:
    """Enhanced comparison wrapper around structured diffs."""

    left_ref: str
    right_ref: str
    changed_fields: tuple[Mapping[str, object], ...] = ()
    added_sections: tuple[str, ...] = ()
    removed_sections: tuple[str, ...] = ()
    table_differences: tuple[Mapping[str, object], ...] = ()
    summary: Mapping[str, object] = field(default_factory=dict)
    unchanged: bool = False
    profile_version: str = COMPARISON_PROFILE_VERSION
    severity: str = "info"
    evidence: tuple[str, ...] = ()
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "changed_fields", tuple(dict(x) for x in (self.changed_fields or ())))
        object.__setattr__(self, "added_sections", tuple(self.added_sections or ()))
        object.__setattr__(self, "removed_sections", tuple(self.removed_sections or ()))
        object.__setattr__(
            self, "table_differences", tuple(dict(x) for x in (self.table_differences or ()))
        )
        object.__setattr__(self, "summary", _meta(self.summary))
        object.__setattr__(self, "evidence", tuple(self.evidence or ()))

    @classmethod
    def from_document_comparison(cls, result, *, severity: str = "info", evidence: tuple[str, ...] = ()):
        return cls(
            left_ref=result.left_ref,
            right_ref=result.right_ref,
            changed_fields=result.changed_fields,
            added_sections=result.added_sections,
            removed_sections=result.removed_sections,
            table_differences=result.table_differences,
            summary=dict(result.summary),
            unchanged=result.unchanged,
            severity=severity if not result.unchanged else "info",
            evidence=evidence,
        )


@dataclass(frozen=True)
class ReconciliationProfile:
    profile_id: str
    version: str = RECONCILIATION_PROFILE_VERSION
    monetary_fields: tuple[str, ...] = ("total", "amount", "subtotal", "vat_amount")
    date_fields: tuple[str, ...] = ("date",)
    identifier_fields: tuple[str, ...] = ()
    role_pairs: tuple[tuple[str, str], ...] = ()
    monetary_tolerance: Decimal = Decimal("0")
    require_all_roles: bool = True
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "monetary_fields", tuple(self.monetary_fields or ()))
        object.__setattr__(self, "date_fields", tuple(self.date_fields or ()))
        object.__setattr__(self, "identifier_fields", tuple(self.identifier_fields or ()))
        object.__setattr__(self, "role_pairs", tuple(self.role_pairs or ()))
        tol = self.monetary_tolerance
        if not isinstance(tol, Decimal):
            object.__setattr__(self, "monetary_tolerance", Decimal(str(tol)))


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    severity: str
    field: str = ""
    left_role: str = ""
    right_role: str = ""
    left_value: object = None
    right_value: object = None
    message: str = ""
    schema_version: str = PLATFORM_SCHEMA_VERSION


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    issues: tuple[ReconciliationIssue, ...] = ()
    matched_fields: tuple[str, ...] = ()
    profile_id: str = ""
    profile_version: str = RECONCILIATION_PROFILE_VERSION
    roles: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        if self.status not in RECONCILIATION_STATUSES:
            object.__setattr__(self, "status", RECON_INSUFFICIENT_DATA)
        object.__setattr__(self, "issues", tuple(self.issues or ()))
        object.__setattr__(self, "matched_fields", tuple(self.matched_fields or ()))
        object.__setattr__(self, "roles", tuple(self.roles or ()))
        object.__setattr__(self, "evidence", tuple(self.evidence or ()))


@dataclass(frozen=True)
class DocumentTemplate:
    template_id: str
    version: str
    tenant_id: str
    output_format: str
    required_fields: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    schema_version: str = PLATFORM_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "required_fields", tuple(self.required_fields or ()))
        object.__setattr__(self, "optional_fields", tuple(self.optional_fields or ()))
        object.__setattr__(self, "sections", tuple(self.sections or ()))
