"""document.large_extract workflow definition + handlers."""

from __future__ import annotations

from documents.errors import DocumentError
from documents.intelligence.large import build_large_doc_plan
from documents.models import DOC_PDF, STATUS_PARSED, STATUS_PARTIAL
from documents.store import _clone
from memory.models import utc_now
from workflow.definition import (
    FAILURE_FAIL_WORKFLOW,
    FAILURE_RETRY,
    STEP_TYPE_HANDLER,
    StepResult,
    StepRetryPolicy,
    WorkflowDefinition,
    WorkflowStep,
)


def large_extract_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="document.large_extract",
        version="1",
        timeout_seconds=3600.0,
        steps=(
            WorkflowStep(step_id="doc_large_prepare", step_type=STEP_TYPE_HANDLER),
            WorkflowStep(
                step_id="doc_large_extract",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("doc_large_prepare",),
                retry_policy=StepRetryPolicy(
                    max_attempts=3,
                    base_delay_seconds=0.05,
                    backoff_mode="fixed",
                ),
                failure_policy=FAILURE_RETRY,
            ),
            WorkflowStep(
                step_id="doc_large_merge",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("doc_large_extract",),
            ),
            WorkflowStep(
                step_id="doc_large_finalize",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("doc_large_merge",),
                failure_policy=FAILURE_FAIL_WORKFLOW,
            ),
        ),
    )


def _engine_services(ctx):
    platform = ctx["platform"]
    engine = getattr(platform, "workflow_engine", None)
    doc_svc = getattr(engine, "document_service", None) if engine else None
    intel = getattr(engine, "document_intelligence", None) if engine else None
    return doc_svc, intel


def _prepare_result(state) -> dict:
    rec = state.step("doc_large_prepare")
    if rec is None:
        return {}
    return dict(rec.metadata.get("result") or {})


async def document_large_extract_handler(ctx) -> StepResult:
    step = ctx["step"]
    state = ctx["state"]
    meta = dict(state.metadata or {})
    document_id = str(meta.get("document_id") or "")
    tenant_id = str(meta.get("tenant_id") or "legacy-default")
    doc_svc, intel = _engine_services(ctx)

    if step.step_id == "doc_large_prepare":
        page_count = int(meta.get("page_count") or 1)
        batch_size = 10
        if intel is not None:
            batch_size = int(getattr(intel.large_policy, "pages_per_batch", 10) or 10)
        plan = build_large_doc_plan(
            document_id=document_id,
            tenant_id=tenant_id,
            page_count=max(1, page_count),
            batch_size=batch_size,
        )
        return StepResult(
            ok=True,
            data={
                "batch_count": plan["batch_count"],
                "batches": plan["batches"],
                "page_count": page_count,
            },
            result_ref=f"document:{document_id}",
        )

    if step.step_id == "doc_large_extract":
        if doc_svc is None:
            raise DocumentError("document_store_unavailable")
        blob = doc_svc.store.get_blob(document_id) if hasattr(doc_svc.store, "get_blob") else None
        if not blob:
            raise DocumentError("document_store_unavailable")
        row = doc_svc.store.get(document_id)
        if row is None:
            raise DocumentError("document_access_denied")
        prep = _prepare_result(state)
        batches = list(prep.get("batches") or meta.get("batches") or ())
        filename = str(meta.get("filename") or row.filename_safe or "document.bin")
        if row.document_type == DOC_PDF and intel is not None:
            parsed = intel.parse_pdf_to_parsed_document(
                document_id=document_id,
                data=blob,
                filename=filename,
                limits=getattr(doc_svc, "limits", None),
                tenant_id=tenant_id,
            )
        else:
            parser = doc_svc.registry.get_parser(row.document_type)
            parsed = parser.parse(
                document_id=document_id,
                data=blob,
                filename=filename,
                limits=doc_svc.limits,
            )
        saved = doc_svc._persist_parsed(row, parsed)  # noqa: SLF001
        return StepResult(
            ok=True,
            data={
                "chunk_count": saved.chunk_count,
                "page_count": saved.page_count,
                "batch_count": len(batches),
                "parser_id": parsed.parser_id,
            },
            result_ref=f"document:{document_id}",
        )

    if step.step_id == "doc_large_merge":
        if doc_svc is None:
            raise DocumentError("document_store_unavailable")
        chunks = doc_svc.store.list_chunks(document_id)
        ordered = sorted(chunks, key=lambda c: (c.ordinal, c.chunk_id))
        return StepResult(
            ok=True,
            data={
                "merged_chunks": len(ordered),
                "order_ok": all(
                    ordered[i].ordinal <= ordered[i + 1].ordinal
                    for i in range(len(ordered) - 1)
                )
                if len(ordered) > 1
                else True,
            },
            result_ref=f"document:{document_id}",
        )

    if step.step_id == "doc_large_finalize":
        if doc_svc is None:
            raise DocumentError("document_store_unavailable")
        row = doc_svc.store.get(document_id)
        if row is not None:
            meta_safe = {
                **dict(row.metadata_safe),
                "extraction_mode": "async_complete",
                "workflow_id": state.workflow_id,
            }
            status = STATUS_PARTIAL if row.warnings else STATUS_PARSED
            updated = _clone(
                row,
                status=status,
                metadata_safe=meta_safe,
                updated_at=utc_now(),
            )
            try:
                doc_svc.store.update(updated, expected_version=row.version)
            except Exception:
                pass
        if hasattr(doc_svc.store, "delete_blob"):
            try:
                doc_svc.store.delete_blob(document_id)
            except Exception:
                pass
        return StepResult(
            ok=True,
            data={"document_id": document_id, "status": "completed"},
            result_ref=f"document:{document_id}",
        )

    return StepResult(ok=True, data={"step_id": step.step_id})


def register_document_workflows(definitions, platform) -> None:
    definitions.register(large_extract_definition())
    for step_id in (
        "doc_large_prepare",
        "doc_large_extract",
        "doc_large_merge",
        "doc_large_finalize",
    ):
        platform.register_handler(step_id, document_large_extract_handler)
