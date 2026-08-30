"""DocumentPlanner — trusted TaskQueue metadata stamping for document workloads.

Large OCR / large PDF / bulk compare-reconcile-generate ALWAYS stamp batch
admission metadata so they cannot run on the interactive pool even if the
caller forces an interactive hint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from documents.errors import DOCUMENT_OCR_BATCH_REQUIRED, DocumentError
from documents.platform_models import (
    DOCUMENT_OPERATIONS,
    OP_COMPARE,
    OP_GENERATE,
    OP_OCR,
    OP_RECONCILE,
    PLATFORM_SCHEMA_VERSION,
    TRUSTED_JOB_DOCUMENT_BULK,
    TRUSTED_JOB_DOCUMENT_LARGE,
    TRUSTED_JOB_DOCUMENT_OCR,
    DocumentProcessingJob,
    new_id,
    utc_now,
)
from security.tenant import require_tenant_id
from task_queue.lanes import (
    LANE_BULK,
    WORKLOAD_BATCH,
    WORKLOAD_NORMAL,
    classify_workload,
)

# Conservative thresholds — hard stamp, not hint-only.
LARGE_OCR_PAGES = 5
LARGE_DOC_BYTES = 5 * 1024 * 1024  # 5 MiB
BULK_OPS = frozenset({OP_COMPARE, OP_RECONCILE, OP_GENERATE})


@dataclass(frozen=True)
class PlannedDocument:
    job: DocumentProcessingJob
    trusted_metadata: Mapping[str, object]
    execution_lane: str
    workload_class: str
    enqueue: bool
    reason: str = ""


def assert_hard_batch_admission(trusted_metadata: Mapping[str, object]) -> None:
    """Verify trusted keys force bulk/batch — mirrors acquisition hard stamp."""
    meta = dict(trusted_metadata or {})
    stamped = classify_workload(metadata=meta)
    jt = str(meta.get("trusted_job_type") or "")
    if jt in {
        TRUSTED_JOB_DOCUMENT_OCR,
        TRUSTED_JOB_DOCUMENT_LARGE,
        TRUSTED_JOB_DOCUMENT_BULK,
    }:
        if stamped.lane != LANE_BULK or str(meta.get("workload_class")) != WORKLOAD_BATCH:
            raise RuntimeError("document_batch_admission_stamp_failed")
        if str(meta.get("execution_lane")) != LANE_BULK:
            raise RuntimeError("document_batch_admission_stamp_failed")


def _must_batch(
    *,
    operations: Sequence[str],
    page_count: int | None,
    byte_size: int | None,
    bulk: bool,
) -> tuple[bool, str]:
    ops = {str(o).lower() for o in operations}
    pages = int(page_count) if page_count is not None else None
    size = int(byte_size) if byte_size is not None else None

    if bulk:
        return True, TRUSTED_JOB_DOCUMENT_BULK
    if OP_OCR in ops and pages is not None and pages >= LARGE_OCR_PAGES:
        return True, TRUSTED_JOB_DOCUMENT_OCR
    if pages is not None and pages >= LARGE_OCR_PAGES:
        return True, TRUSTED_JOB_DOCUMENT_LARGE
    if size is not None and size >= LARGE_DOC_BYTES:
        return True, TRUSTED_JOB_DOCUMENT_LARGE
    if ops & BULK_OPS and (bulk or (pages is not None and pages >= LARGE_OCR_PAGES)):
        return True, TRUSTED_JOB_DOCUMENT_BULK
    if ops & BULK_OPS and bulk:
        return True, TRUSTED_JOB_DOCUMENT_BULK
    return False, ""


def plan_document_job(
    *,
    document_id: str,
    tenant_id: str,
    operations: Sequence[str],
    version_id: str = "",
    execution_id: str = "",
    workflow_id: str = "",
    task_id: str = "",
    page_count: int | None = None,
    byte_size: int | None = None,
    bulk: bool = False,
    force_interactive_hint: bool = False,
    idempotency_key: str = "",
    profile_version: str = PLATFORM_SCHEMA_VERSION,
    pinned_providers: Mapping[str, object] | None = None,
    pinned_profiles: Mapping[str, object] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> PlannedDocument:
    """Build DocumentProcessingJob with HARD-stamped trusted TaskQueue metadata.

    ``force_interactive_hint`` from caller payload is ignored when large OCR /
    large PDF / bulk ops require batch — trusted_job_type + workload_class=batch
    always win.
    """
    tid = require_tenant_id(tenant_id)
    ops = tuple(
        o for o in (str(x).lower() for x in operations) if o in DOCUMENT_OPERATIONS
    )
    if not ops:
        ops = ("ingest", "classify", "extract")

    # Explicit bulk flag or multi-doc compare/reconcile/generate → bulk job type.
    want_bulk = bool(bulk) or bool(set(ops) & BULK_OPS and (
        (page_count is not None and int(page_count) >= LARGE_OCR_PAGES)
        or (byte_size is not None and int(byte_size) >= LARGE_DOC_BYTES)
        or bulk
    ))
    # Multi-document reconcile/compare/generate always batch when marked bulk.
    if bulk and set(ops) & BULK_OPS:
        want_bulk = True

    batch, job_type = _must_batch(
        operations=ops,
        page_count=page_count,
        byte_size=byte_size,
        bulk=want_bulk or bulk,
    )
    # Caller interactive hint MUST NOT win over trusted stamp.
    _ = force_interactive_hint

    if batch:
        if not job_type:
            if OP_OCR in ops:
                job_type = TRUSTED_JOB_DOCUMENT_OCR
            elif set(ops) & BULK_OPS:
                job_type = TRUSTED_JOB_DOCUMENT_BULK
            else:
                job_type = TRUSTED_JOB_DOCUMENT_LARGE
        trusted_metadata = {
            "trusted_job_type": job_type,
            "workload_class": WORKLOAD_BATCH,
            "execution_lane": LANE_BULK,
            "profile_version": profile_version,
            "document_operations": list(ops),
        }
        if metadata:
            # Never allow payload to override trusted keys.
            for k, v in dict(metadata).items():
                if k not in {"trusted_job_type", "workload_class", "execution_lane"}:
                    trusted_metadata[k] = v
        assert_hard_batch_admission(trusted_metadata)
        workload = WORKLOAD_BATCH
        lane = LANE_BULK
        enqueue = True
        reason = "batch_required"
    else:
        trusted_job_type = "document"
        trusted_metadata = {
            "trusted_job_type": trusted_job_type,
            "workload_class": WORKLOAD_NORMAL,
            "execution_lane": "default",
            "profile_version": profile_version,
            "document_operations": list(ops),
        }
        stamped = classify_workload(metadata=trusted_metadata)
        workload = stamped.name
        lane = stamped.lane
        enqueue = False
        reason = "inline_ok"

    now = utc_now()
    job = DocumentProcessingJob(
        job_id=new_id("djob-"),
        document_id=document_id,
        version_id=version_id,
        tenant_id=tid,
        execution_id=execution_id,
        workflow_id=workflow_id,
        task_id=task_id,
        operations=ops,
        workload_class=workload,
        execution_lane=lane,
        profile_version=profile_version,
        status="pending",
        stage="ingest",
        checkpoint={"stage": "planned"},
        created_at=now,
        updated_at=now,
        idempotency_key=idempotency_key or f"doc:{tid}:{document_id}:{','.join(ops)}",
        pinned_providers=dict(pinned_providers or {}),
        pinned_profiles=dict(pinned_profiles or {}),
    )
    return PlannedDocument(
        job=job,
        trusted_metadata=trusted_metadata,
        execution_lane=lane,
        workload_class=workload,
        enqueue=enqueue,
        reason=reason,
    )


class DocumentPlanner:
    """Thin façade matching AcquisitionPlanner style."""

    def plan(self, **kwargs) -> PlannedDocument:
        return plan_document_job(**kwargs)


def requires_batch_ocr_admission(
    *,
    page_count: int | None,
    byte_size: int | None = None,
    operations: Sequence[str] = (OP_OCR,),
) -> bool:
    """True when OCR workloads must not run on the sync/interactive path."""
    batch, _ = _must_batch(
        operations=tuple(operations),
        page_count=page_count,
        byte_size=byte_size,
        bulk=False,
    )
    return batch


def resolve_sync_ocr_page_count(data: bytes, *, filename: str = "") -> int | None:
    """Trusted page count for sync OCR admission; None when not safely known."""
    name = (filename or "").lower()
    if data.startswith(b"%PDF") or name.endswith(".pdf"):
        from documents.intelligence.pdf_ocr import peek_pdf_page_count

        try:
            return peek_pdf_page_count(data)
        except DocumentError:
            return None
    if name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif")):
        return 1
    return None


def assert_sync_ocr_allowed(
    *,
    page_count: int | None,
    byte_size: int | None = None,
    require_known_size: bool = True,
) -> None:
    """Fail closed when synchronous OCR is not permitted."""
    if require_known_size and page_count is None and byte_size is None:
        raise DocumentError(DOCUMENT_OCR_BATCH_REQUIRED)
    if page_count is None and byte_size is not None and byte_size >= LARGE_DOC_BYTES:
        raise DocumentError(DOCUMENT_OCR_BATCH_REQUIRED)
    if requires_batch_ocr_admission(page_count=page_count, byte_size=byte_size):
        raise DocumentError(DOCUMENT_OCR_BATCH_REQUIRED)
