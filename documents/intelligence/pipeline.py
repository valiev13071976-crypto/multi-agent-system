"""Multi-stage document processing pipeline with durable checkpoints."""

from __future__ import annotations

from dataclasses import replace

from documents.intelligence.classify import classify_document
from documents.intelligence.extraction import extract_structured, extract_structured_with_schema
from documents.intelligence.ocr_plan import plan_ocr
from documents.intelligence.validation import validate_structured
from documents.observability import get_observer
from documents.platform_models import (
    CLASSIFIER_VERSION,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_RUNNING,
    STAGE_CLASSIFY,
    STAGE_DONE,
    STAGE_EXTRACT,
    STAGE_INGEST,
    STAGE_OCR,
    STAGE_VALIDATE,
    DocumentResult,
    DocumentVersion,
    ExtractionSchema,
    new_id,
    utc_now,
)
from documents.planner import plan_document_job
from security.tenant import require_tenant_id


def _checkpoint(job, *, stage: str, **extra):
    cp = dict(job.checkpoint)
    cp["stage"] = stage
    cp.update(extra)
    return replace(job, stage=stage, checkpoint=cp, updated_at=utc_now(), status=JOB_RUNNING)


def run_document_pipeline(
    intel,
    *,
    document_id: str,
    tenant_id: str,
    content=None,
    filename: str = "",
    schema: ExtractionSchema | None = None,
    operations: tuple[str, ...] = ("ingest", "ocr", "classify", "extract"),
    page_count: int | None = None,
    byte_size: int | None = None,
    force_interactive_hint: bool = False,
    resume_job_id: str | None = None,
    execution_id: str = "",
    workflow_id: str = "",
    task_id: str = "",
):
    """Deterministic ingest→OCR-plan→classify→extract→validate pipeline.

    Resume skips completed stages based on durable job.checkpoint.
    """
    tid = require_tenant_id(tenant_id)
    obs = get_observer()
    store = getattr(getattr(intel, "documents", None), "store", None)

    job = None
    if resume_job_id and store is not None:
        job = store.get_processing_job(resume_job_id, tenant_id=tid)
    if job is None:
        planned = plan_document_job(
            document_id=document_id,
            tenant_id=tid,
            operations=operations,
            page_count=page_count,
            byte_size=byte_size,
            force_interactive_hint=force_interactive_hint,
            execution_id=execution_id,
            workflow_id=workflow_id,
            task_id=task_id,
            pinned_providers={"ocr": getattr(getattr(intel, "ocr", None), "provider_id", "")},
            pinned_profiles={
                "classifier": CLASSIFIER_VERSION,
                "extraction": "extract_v1",
            },
        )
        job = planned.job
        trusted_metadata = planned.trusted_metadata
    else:
        trusted_metadata = {
            "trusted_job_type": "document",
            "workload_class": job.workload_class,
            "execution_lane": job.execution_lane,
        }

    if store is not None:
        store.save_processing_job(job)

    obs.on_processing_started(job_id=job.job_id, tenant_id=tid, stage=job.stage)

    completed = set(job.checkpoint.get("completed_stages") or ())
    warnings: list[str] = []
    errors: list[str] = []
    version_id = job.version_id or new_id("dver-")

    # Stage: ingest / native content
    if STAGE_INGEST not in completed:
        if content is None:
            content = intel.extract_content(document_id, tenant_id=tid)
        obs.on_native_extracted(
            document_id=document_id,
            tenant_id=tid,
            char_count=len(content.text or ""),
        )
        completed.add(STAGE_INGEST)
        job = _checkpoint(job, stage=STAGE_INGEST, completed_stages=sorted(completed))
        if store is not None:
            store.save_processing_job(job)
            store.save_document_version(
                DocumentVersion(
                    document_id=document_id,
                    version_id=version_id,
                    artifact_id=document_id,
                    content_hash=str((content.metadata or {}).get("content_hash") or ""),
                    transformation_reason="ingest",
                    producing_operation="ingest",
                    producing_tool_or_model="documents.pipeline",
                    provenance={"job_id": job.job_id},
                )
            )
    else:
        content = content or intel.extract_content(document_id, tenant_id=tid)

    # Stage: OCR plan (and optional OCR)
    ocr_plan = None
    if STAGE_OCR not in completed and "ocr" in job.operations:
        provider = getattr(intel.ocr, "provider_id", "")
        available = bool(getattr(intel.ocr, "available", False))
        ocr_plan = plan_ocr(
            native_text=content.text,
            page_count=page_count or len(content.pages or ()),
            page_stats=[
                {"char_count": len(str(p.get("text") or ""))}
                if isinstance(p, dict)
                else {"char_count": 0}
                for p in (content.pages or ())
            ]
            or None,
            provider=provider,
            provider_available=available,
        )
        obs.on_ocr(
            status=ocr_plan.status,
            document_id=document_id,
            tenant_id=tid,
            page_count=ocr_plan.page_count,
        )
        if ocr_plan.status == "required" and available:
            # Bounded single-shot OCR on provided content bytes is left to caller;
            # mark performed when content already OCR-derived.
            if content.extraction_method == "ocr":
                from documents.intelligence.ocr_plan import plan_ocr as _p

                ocr_plan = _p(
                    native_text=content.text,
                    ocr_already_performed=True,
                    provider=provider,
                    provider_available=available,
                )
        obs.on_ocr(
            status=ocr_plan.status if ocr_plan.status != "required" else "performed",
            document_id=document_id,
            tenant_id=tid,
            page_count=ocr_plan.page_count,
        )
        completed.add(STAGE_OCR)
        job = _checkpoint(
            job,
            stage=STAGE_OCR,
            completed_stages=sorted(completed),
            ocr_status=ocr_plan.status,
        )
        if store is not None:
            store.save_processing_job(job)
    elif "ocr" in completed or STAGE_OCR in completed:
        from documents.platform_models import OCRPlanDecision

        ocr_plan = OCRPlanDecision(
            status=str(job.checkpoint.get("ocr_status") or "not_required"),
            reason="resumed",
            provider=str((job.pinned_providers or {}).get("ocr") or ""),
        )

    # Stage: classify
    classification = None
    if STAGE_CLASSIFY not in completed and "classify" in job.operations:
        classification = classify_document(content.text or "", filename=filename)
        # Pin classifier version from job
        if job.pinned_profiles.get("classifier"):
            classification = replace(
                classification,
                classifier_version=str(job.pinned_profiles.get("classifier")),
            )
        obs.on_classified(
            document_id=document_id,
            tenant_id=tid,
            doc_class=classification.doc_class,
        )
        completed.add(STAGE_CLASSIFY)
        job = _checkpoint(
            job,
            stage=STAGE_CLASSIFY,
            completed_stages=sorted(completed),
            doc_class=classification.doc_class,
        )
        if store is not None:
            store.save_processing_job(job)

    # Stage: extract
    structured = None
    field_values = ()
    if STAGE_EXTRACT not in completed and "extract" in job.operations:
        if schema is not None:
            structured, field_values = extract_structured_with_schema(
                content, schema, filename=filename
            )
        else:
            doc_type = None
            if classification and classification.doc_class not in {"", "unknown"}:
                doc_type = classification.doc_class
            structured = extract_structured(
                content, document_type=doc_type, filename=filename
            )
        obs.on_extracted(
            document_id=document_id,
            tenant_id=tid,
            field_count=len(getattr(structured, "fields", {}) or {}),
        )
        completed.add(STAGE_EXTRACT)
        job = _checkpoint(job, stage=STAGE_EXTRACT, completed_stages=sorted(completed))
        if store is not None:
            store.save_processing_job(job)

    # Stage: validate
    validation = {"ok": True, "errors": ()}
    if STAGE_VALIDATE not in completed and structured is not None:
        vr = validate_structured(structured)
        validation = {"ok": vr.ok, "errors": tuple(vr.errors)}
        obs.on_validated(document_id=document_id, tenant_id=tid, ok=vr.ok)
        completed.add(STAGE_VALIDATE)
        job = _checkpoint(job, stage=STAGE_VALIDATE, completed_stages=sorted(completed))
        if store is not None:
            store.save_processing_job(job)

    status = JOB_COMPLETED
    if errors:
        status = JOB_FAILED
    elif warnings or (ocr_plan and ocr_plan.status == "partial"):
        status = "partial"

    job = replace(
        job,
        status=status,
        stage=STAGE_DONE,
        version_id=version_id,
        checkpoint={**dict(job.checkpoint), "completed_stages": sorted(completed), "stage": STAGE_DONE},
        updated_at=utc_now(),
        completed_at=utc_now(),
    )
    if store is not None:
        store.save_processing_job(job)

    obs.on_processing_started(job_id=job.job_id, tenant_id=tid, stage=STAGE_DONE)

    result = DocumentResult(
        document_id=document_id,
        version_id=version_id,
        status=status,
        text_ref=f"doc:{document_id}:text",
        blocks=tuple(content.sections or ()) if content else (),
        tables=tuple(content.tables or ()) if content else (),
        classification={
            "doc_class": getattr(classification, "doc_class", None),
            "status": getattr(classification, "status", None),
            "classifier_version": getattr(classification, "classifier_version", None),
            "confidence": getattr(classification, "confidence", None),
            "evidence": list(getattr(classification, "evidence", ()) or ()),
        }
        if classification
        else {},
        fields={
            fv.name: {"status": fv.status, "value": fv.value, "provenance": dict(fv.provenance)}
            for fv in field_values
        }
        if field_values
        else (dict(structured.fields) if structured else {}),
        validation=validation,
        provenance={
            "job_id": job.job_id,
            "workload_class": job.workload_class,
            "execution_lane": job.execution_lane,
            "trusted_metadata": dict(trusted_metadata),
            "pinned_profiles": dict(job.pinned_profiles),
            "execution_id": execution_id,
            "workflow_id": workflow_id,
            "task_id": task_id,
        },
        warnings=tuple(warnings),
        errors=tuple(errors),
    )
    return job, result, structured, ocr_plan
