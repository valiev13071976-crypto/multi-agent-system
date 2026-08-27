"""document.large_extract workflow — bounded batch execution + merge."""

from __future__ import annotations

import uuid

from documents.errors import DocumentError
from documents.intelligence.contracts import DocumentContent
from documents.intelligence.large import build_large_doc_plan
from documents.intelligence.pdf_ocr import build_pdf_document_content, content_to_parsed_document
from documents.models import (
    DOC_PDF,
    STATUS_PARSED,
    STATUS_PARTIAL,
    TextBlock,
    ParsedDocument,
    content_hash_text,
)
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
                    max_attempts=5,
                    base_delay_seconds=0.01,
                    backoff_mode="fixed",
                    retryable_error_classes=("ocr_failed", "extraction_failed", "DocumentError"),
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


def _completed_batch_indices(store, document_id: str) -> set[int]:
    partials = store.list_extract_partials(document_id) if hasattr(store, "list_extract_partials") else {}
    return {
        int(idx)
        for idx, payload in partials.items()
        if dict(payload).get("status") == "completed"
    }


def _next_batch(batches: list, completed: set[int]) -> dict | None:
    for b in batches:
        idx = int(b.get("batch_index", -1))
        if idx not in completed:
            return dict(b)
    return None


def _extract_pdf_batch(*, intel, doc_svc, document_id, blob, filename, tenant_id, batch) -> dict:
    page_start = int(batch["page_start"])
    page_end = int(batch["page_end"])
    if page_end - page_start + 1 > int(getattr(intel.large_policy, "pages_per_batch", 10) or 10):
        raise DocumentError("document_too_many_pages")
    content = build_pdf_document_content(
        document_id=document_id,
        data=blob,
        filename=filename,
        ocr_provider=intel.ocr,
        rasterizer=intel.rasterizer,
        limits=getattr(doc_svc, "limits", None),
        page_start=page_start,
        page_end=page_end,
    )
    pages = [dict(p) for p in content.pages]
    checksum = content_hash_text("\n".join(str(p.get("text") or "") for p in pages))
    return {
        "batch_index": int(batch["batch_index"]),
        "kind": "pdf_pages",
        "page_start": page_start,
        "page_end": page_end,
        "pages": pages,
        "status": "completed",
        "checksum": checksum,
        "extraction_method": content.extraction_method,
        "warnings": list(content.warnings),
        "bounded": True,
    }


def _extract_text_batch(*, blob, batch) -> dict:
    text = blob.decode("utf-8", errors="replace")
    start = int(batch["char_start"])
    end = int(batch["char_end"])
    slice_text = text[start:end]
    checksum = content_hash_text(slice_text)
    return {
        "batch_index": int(batch["batch_index"]),
        "kind": "text_range",
        "char_start": start,
        "char_end": end,
        "pages": [
            {
                "page": int(batch["batch_index"]) + 1,
                "source_location": f"text:chars:{start}-{end}",
                "extraction_method": "text_slice",
                "text": slice_text,
                "text_preview": slice_text[:500],
                "content_hash": checksum,
            }
        ],
        "status": "completed",
        "checksum": checksum,
        "extraction_method": "text_slice",
        "warnings": [],
        "bounded": True,
    }


def _extract_fallback(*, doc_svc, row, blob, filename) -> dict:
    parser = doc_svc.registry.get_parser(row.document_type)
    parsed = parser.parse(
        document_id=row.document_id,
        data=blob,
        filename=filename,
        limits=doc_svc.limits,
    )
    pages = []
    for b in parsed.text_blocks:
        pages.append(
            {
                "page": b.page or (b.ordinal + 1),
                "source_location": b.source_location,
                "extraction_method": "full_document_fallback",
                "text": b.text,
                "text_preview": (b.text or "")[:500],
                "content_hash": b.content_hash,
            }
        )
    checksum = content_hash_text("\n".join(p["text"] for p in pages))
    return {
        "batch_index": 0,
        "kind": "full_document_fallback",
        "pages": pages,
        "status": "completed",
        "checksum": checksum,
        "extraction_method": "full_document_fallback",
        "warnings": list(parsed.warnings) + ["partial_parse_unsupported"],
        "bounded": False,
        "fallback": "parser_lacks_partial_range",
        "parser_id": parsed.parser_id,
    }


async def document_large_extract_handler(ctx) -> StepResult:
    step = ctx["step"]
    state = ctx["state"]
    meta = dict(state.metadata or {})
    document_id = str(meta.get("document_id") or "")
    tenant_id = str(meta.get("tenant_id") or "legacy-default")
    doc_svc, intel = _engine_services(ctx)

    if step.step_id == "doc_large_prepare":
        page_count = int(meta.get("page_count") or 1)
        doc_type = str(meta.get("document_type") or "pdf")
        batch_size = 10
        max_chars = 80_000
        if intel is not None:
            batch_size = int(getattr(intel.large_policy, "pages_per_batch", 10) or 10)
            max_chars = int(getattr(intel.large_policy, "max_text_chars_per_batch", 80_000) or 80_000)
        plan = build_large_doc_plan(
            document_id=document_id,
            tenant_id=tenant_id,
            page_count=max(1, page_count),
            batch_size=batch_size,
            document_type=doc_type,
            text_chars=int(meta.get("text_chars") or 0),
            max_text_chars_per_batch=max_chars,
        )
        return StepResult(
            ok=True,
            data={
                "batch_count": plan["batch_count"],
                "batches": plan["batches"],
                "page_count": plan.get("page_count") or page_count,
                "strategy": plan.get("strategy"),
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
        if not batches:
            raise DocumentError("structured_extraction_failed")
        completed = _completed_batch_indices(doc_svc.store, document_id)
        batch = _next_batch(batches, completed)
        if batch is None:
            return StepResult(
                ok=True,
                data={
                    "batch_count": len(batches),
                    "completed_batches": sorted(completed),
                    "batches_remaining": 0,
                },
                result_ref=f"document:{document_id}",
            )

        filename = str(meta.get("filename") or row.filename_safe or "document.bin")
        kind = str(batch.get("kind") or "pdf_pages")
        try:
            if kind == "pdf_pages":
                if intel is None:
                    raise DocumentError("document_store_unavailable")
                payload = _extract_pdf_batch(
                    intel=intel,
                    doc_svc=doc_svc,
                    document_id=document_id,
                    blob=blob,
                    filename=filename,
                    tenant_id=tenant_id,
                    batch=batch,
                )
            elif kind == "text_range":
                payload = _extract_text_batch(blob=blob, batch=batch)
            else:
                payload = _extract_fallback(
                    doc_svc=doc_svc, row=row, blob=blob, filename=filename
                )
        except DocumentError as exc:
            # Persist failed batch marker without wiping completed ones
            fail_payload = {
                "batch_index": int(batch["batch_index"]),
                "kind": kind,
                "status": "failed",
                "error": exc.reason,
                "page_start": batch.get("page_start"),
                "page_end": batch.get("page_end"),
            }
            doc_svc.store.save_extract_partial(
                document_id, int(batch["batch_index"]), fail_payload
            )
            raise

        doc_svc.store.save_extract_partial(document_id, int(batch["batch_index"]), payload)
        completed.add(int(batch["batch_index"]))
        remaining = len(batches) - len(completed)
        data = {
            "batch_index": int(batch["batch_index"]),
            "page_start": payload.get("page_start"),
            "page_end": payload.get("page_end"),
            "checksum": payload.get("checksum"),
            "extraction_method": payload.get("extraction_method"),
            "bounded": bool(payload.get("bounded")),
            "completed_batches": sorted(completed),
            "batches_remaining": remaining,
            "batch_count": len(batches),
        }
        if remaining > 0:
            data["continue_step"] = True
        return StepResult(ok=True, data=data, result_ref=f"document:{document_id}")

    if step.step_id == "doc_large_merge":
        if doc_svc is None:
            raise DocumentError("document_store_unavailable")
        row = doc_svc.store.get(document_id)
        if row is None:
            raise DocumentError("document_access_denied")
        partials = doc_svc.store.list_extract_partials(document_id)
        if not partials:
            raise DocumentError("structured_extraction_failed")
        # Ordered merge by batch_index then page
        ordered_pages = []
        seen_hashes = set()
        warnings = []
        methods = set()
        for idx in sorted(partials.keys()):
            payload = dict(partials[idx])
            if payload.get("status") != "completed":
                raise DocumentError(str(payload.get("error") or "extraction_failed"))
            methods.add(str(payload.get("extraction_method") or ""))
            warnings.extend(list(payload.get("warnings") or ()))
            for page in payload.get("pages") or ():
                p = dict(page)
                ch = str(p.get("content_hash") or "")
                # Drop exact duplicate overlap by content_hash+page
                key = (int(p.get("page") or 0), ch)
                if key in seen_hashes:
                    continue
                seen_hashes.add(key)
                ordered_pages.append(p)
        ordered_pages.sort(key=lambda p: (int(p.get("page") or 0), str(p.get("source_location") or "")))
        text = "\n\n".join(str(p.get("text") or "") for p in ordered_pages if p.get("text"))
        content = DocumentContent(
            document_id=document_id,
            text=text,
            pages=tuple(ordered_pages),
            metadata={
                "filename": meta.get("filename") or row.filename_safe,
                "page_count": max((int(p.get("page") or 0) for p in ordered_pages), default=0),
                "methods": sorted(m for m in methods if m),
                "merged_batches": len(partials),
            },
            extraction_method="merged_batches",
            warnings=tuple(dict.fromkeys(warnings)),
        )
        parsed = content_to_parsed_document(content, parser_id="large_extract_merge_v1")
        # Stable ordinals by page order
        blocks = []
        for i, page in enumerate(ordered_pages):
            t = str(page.get("text") or "")
            if not t:
                continue
            blocks.append(
                TextBlock(
                    block_id=str(uuid.uuid4()),
                    ordinal=i,
                    text=t,
                    content_hash=content_hash_text(t),
                    source_location=str(page.get("source_location") or f"page:{page.get('page')}"),
                    page=int(page.get("page") or (i + 1)),
                    metadata_safe={
                        "extraction_method": page.get("extraction_method") or "merged",
                        "batch_merge": True,
                    },
                )
            )
        parsed = ParsedDocument(
            document_id=document_id,
            text_blocks=tuple(blocks),
            tables=(),
            metadata_safe=dict(content.metadata),
            parser_id="large_extract_merge_v1",
            parser_version="1.2.0",
            title=str(content.metadata.get("filename") or row.title or ""),
            pages=int(content.metadata.get("page_count") or len(blocks)),
            warnings=tuple(content.warnings),
            partial=bool(content.warnings),
        )
        saved = doc_svc._persist_parsed(row, parsed)  # noqa: SLF001
        return StepResult(
            ok=True,
            data={
                "merged_chunks": saved.chunk_count,
                "merged_pages": len(ordered_pages),
                "order_ok": True,
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
        if hasattr(doc_svc.store, "clear_extract_partials"):
            try:
                doc_svc.store.clear_extract_partials(document_id)
            except Exception:
                pass
        return StepResult(
            ok=True,
            data={"document_id": document_id, "status": "completed"},
            result_ref=f"document:{document_id}",
        )

    return StepResult(ok=True, data={"step_id": step.step_id})


def register_document_workflows(definitions, platform) -> None:
    # Re-register replaces same type@version if registry allows; else ignore
    try:
        definitions.register(large_extract_definition())
    except Exception:
        pass
    for step_id in (
        "doc_large_prepare",
        "doc_large_extract",
        "doc_large_merge",
        "doc_large_finalize",
    ):
        platform.register_handler(step_id, document_large_extract_handler)
