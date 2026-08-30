"""Document Intelligence Service — extract/OCR/compare/generate/convert/pipeline."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Sequence

from documents.errors import (
    DOCUMENT_ACCESS_DENIED,
    DOCUMENT_OCR_BATCH_REQUIRED,
    DOCUMENT_REQUIRES_OCR,
    GENERATION_FAILED,
    OCR_UNAVAILABLE,
    DocumentError,
)
from documents.intelligence.classify import classify_document, classify_document_text
from documents.intelligence.compare import compare_structured, compare_text_sections
from documents.intelligence.content import content_from_parsed
from documents.intelligence.contracts import (
    DocumentComparisonResult,
    DocumentContent,
    DocumentDescriptor,
    DocumentLinkResult,
    GeneratedDocument,
    StructuredDocument,
)
from documents.intelligence.convert import convert_document
from documents.intelligence.extraction import extract_structured, extract_structured_with_schema
from documents.intelligence.generate import generate_docx, generate_pdf, generate_txt
from documents.intelligence.large import LargeDocumentPolicy, build_large_doc_plan, large_extract_execution_key
from documents.intelligence.linking import link_documents
from documents.intelligence.ocr import NullOCRProvider, build_ocr_provider
from documents.intelligence.ocr_plan import plan_ocr
from documents.intelligence.pdf_ocr import (
    build_pdf_document_content,
    content_to_parsed_document,
    peek_pdf_page_count,
)
from documents.intelligence.raster import NullPdfRasterizer, build_pdf_rasterizer
from documents.intelligence.reconcile import reconcile_documents
from documents.intelligence.validation import validate_structured
from documents.models import SOURCE_SYSTEM, DocumentIngestRequest, ParsedDocument
from documents.observability import get_observer
from documents.planner import (
    PlannedDocument,
    assert_sync_ocr_allowed,
    plan_document_job,
    resolve_sync_ocr_page_count,
)
from documents.platform_models import (
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_RUNNING,
    OP_OCR,
    OCR_REQUIRED,
    STAGE_CLASSIFY,
    STAGE_DONE,
    STAGE_EXTRACT,
    STAGE_INGEST,
    STAGE_OCR,
    STAGE_VALIDATE,
    ComparisonResult,
    DocumentProcessingJob,
    DocumentResult,
    DocumentTemplate,
    DocumentVersion,
    ExtractionSchema,
    ReconciliationProfile,
    ReconciliationResult,
    new_id,
    utc_now,
)
from documents.type_detect import resolve_document_type
from memory.models import MemoryScope
from security.encryption import SENSITIVITY_INTERNAL
from security.tenant import normalize_tenant_id, require_tenant_id, tenants_match


class DocumentIntelligenceService:
    def __init__(
        self,
        document_service=None,
        *,
        ocr_provider=None,
        rasterizer=None,
        large_policy: LargeDocumentPolicy | None = None,
        workflow_runtime=None,
        observer=None,
        store=None,
    ):
        self.documents = document_service
        self.ocr = ocr_provider if ocr_provider is not None else NullOCRProvider()
        self.rasterizer = rasterizer if rasterizer is not None else NullPdfRasterizer()
        self.large_policy = large_policy or LargeDocumentPolicy()
        self.workflow_runtime = workflow_runtime
        self._observer = observer
        self._store = store
        self._structured_cache: dict[tuple[str, str], StructuredDocument] = {}
        self._content_cache: dict[tuple[str, str], DocumentContent] = {}

    @property
    def observer(self):
        return self._observer or get_observer()

    @property
    def store(self):
        if self._store is not None:
            return self._store
        if self.documents is not None:
            return getattr(self.documents, "store", None)
        return None

    def detect_type(self, *, filename: str, data: bytes, media_type: str | None = None) -> tuple[str, str]:
        return resolve_document_type(filename=filename, data=data, declared_media_type=media_type)

    def descriptor_from_record(self, record, *, tenant_id: str) -> DocumentDescriptor:
        tid = normalize_tenant_id(tenant_id or getattr(record.scope, "tenant_ref", None))
        return DocumentDescriptor(
            document_id=record.document_id,
            tenant_id=tid,
            filename=record.filename_safe,
            media_type=record.media_type,
            document_type=record.document_type,
            size=int(record.size_bytes),
            checksum=record.content_hash,
            source_ref=record.source_ref,
            created_at=record.created_at,
            provenance={
                "source_type": record.source_type,
                "parser_version": record.parser_version,
            },
            status=record.status,
        )

    def _scope(self, tenant_id: str, scope: MemoryScope | None = None) -> MemoryScope:
        if scope is not None:
            return scope
        tid = normalize_tenant_id(tenant_id)
        return MemoryScope(scope_type="workspace", scope_id=tid, tenant_ref=tid)

    def _require_doc(self, document_id: str, *, tenant_id: str, scope: MemoryScope | None = None):
        if self.documents is None:
            raise DocumentError(DOCUMENT_ACCESS_DENIED)
        sc = self._scope(tenant_id, scope)
        row = self.documents.get(document_id=document_id, requesting_scope=sc)
        if row is None:
            raise DocumentError(DOCUMENT_ACCESS_DENIED)
        if not tenants_match(row.scope.tenant_ref, tenant_id):
            raise DocumentError(DOCUMENT_ACCESS_DENIED)
        return row, sc

    def _save_job(self, job: DocumentProcessingJob) -> DocumentProcessingJob:
        store = self.store
        if store is not None and hasattr(store, "save_processing_job"):
            return store.save_processing_job(job)
        return job

    def _checkpoint(
        self,
        job: DocumentProcessingJob,
        *,
        stage: str,
        status: str | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> DocumentProcessingJob:
        cp = dict(job.checkpoint)
        cp["stage"] = stage
        if extra:
            cp.update(dict(extra))
        updated = replace(
            job,
            stage=stage,
            status=status or job.status,
            checkpoint=cp,
            updated_at=utc_now(),
        )
        return self._save_job(updated)

    def extract_content(
        self,
        document_id: str,
        *,
        tenant_id: str,
        scope: MemoryScope | None = None,
        parsed: ParsedDocument | None = None,
    ) -> DocumentContent:
        key = (normalize_tenant_id(tenant_id), document_id)
        if key in self._content_cache and parsed is None:
            return self._content_cache[key]
        if parsed is None:
            row, sc = self._require_doc(document_id, tenant_id=tenant_id, scope=scope)
            chunks = self.documents.list_chunks(document_id, requesting_scope=sc)
            text = "\n\n".join(
                (c.content_safe or "") for c in chunks if getattr(c, "content_safe", None)
            )
            content = DocumentContent(
                document_id=document_id,
                text=text,
                metadata={
                    "document_type": row.document_type,
                    "chunk_count": row.chunk_count,
                    "status": row.status,
                },
                extraction_method="chunks",
                confidence="medium",
            )
        else:
            content = content_from_parsed(parsed)
        self._content_cache[key] = content
        return content

    def ocr_bytes(self, data: bytes, *, filename: str = "") -> dict:
        if not getattr(self.ocr, "available", False):
            raise DocumentError(OCR_UNAVAILABLE)
        return self.ocr.recognize(data, filename=filename)

    def ocr_document(
        self,
        document_id: str,
        *,
        tenant_id: str,
        data: bytes | None = None,
        filename: str = "",
    ) -> DocumentContent:
        if data is None:
            raise DocumentError(DOCUMENT_REQUIRES_OCR)
        page_count = resolve_sync_ocr_page_count(data, filename=filename)
        if page_count is None:
            raise DocumentError(DOCUMENT_OCR_BATCH_REQUIRED)
        assert_sync_ocr_allowed(
            page_count=page_count,
            byte_size=len(data),
            require_known_size=False,
        )
        result = self.ocr_bytes(data, filename=filename)
        text = str(result.get("text") or "")
        content = DocumentContent(
            document_id=document_id,
            text=text,
            pages=({"page": 1, "source_location": "ocr:page:1"},),
            metadata={
                "ocr_provider": result.get("provider"),
                "ocr_confidence_raw": result.get("confidence_raw"),
            },
            extraction_method="ocr",
            confidence=str(result.get("confidence_level") or "medium"),
            warnings=tuple(result.get("warnings") or ()),
        )
        self._content_cache[(normalize_tenant_id(tenant_id), document_id)] = content
        return content

    def extract_pdf_with_ocr_fallback(
        self,
        *,
        document_id: str,
        data: bytes,
        filename: str,
        limits: dict | None = None,
        tenant_id: str = "legacy-default",
    ) -> DocumentContent:
        """Text PDF + rasterize scanned pages + OCR; never OCR raw PDF bytes."""
        _ = tenant_id
        content = build_pdf_document_content(
            document_id=document_id,
            data=data,
            filename=filename,
            ocr_provider=self.ocr,
            rasterizer=self.rasterizer,
            limits=limits,
        )
        self._content_cache[(normalize_tenant_id(tenant_id), document_id)] = content
        return content

    def parse_pdf_to_parsed_document(
        self,
        *,
        document_id: str,
        data: bytes,
        filename: str,
        limits: dict | None = None,
        tenant_id: str = "legacy-default",
    ) -> ParsedDocument:
        content = self.extract_pdf_with_ocr_fallback(
            document_id=document_id,
            data=data,
            filename=filename,
            limits=limits,
            tenant_id=tenant_id,
        )
        return content_to_parsed_document(content)

    def peek_pdf_pages(self, data: bytes) -> int:
        return peek_pdf_page_count(data)

    def structured_extract(
        self,
        document_id: str,
        *,
        tenant_id: str,
        content: DocumentContent | None = None,
        document_type: str | None = None,
        filename: str = "",
        schema: ExtractionSchema | None = None,
    ) -> StructuredDocument:
        content = content or self.extract_content(document_id, tenant_id=tenant_id)
        if schema is not None:
            structured = extract_structured_with_schema(content, schema)
        else:
            structured = extract_structured(
                content, document_type=document_type, filename=filename
            )
        self._structured_cache[(normalize_tenant_id(tenant_id), document_id)] = structured
        return structured

    def classify(self, text: str, *, filename: str = "") -> tuple[str, str, tuple[str, ...]]:
        return classify_document_text(text, filename=filename)

    def classify_result(self, text: str, *, filename: str = ""):
        return classify_document(text, filename=filename)

    def compare(
        self,
        left: StructuredDocument | DocumentContent,
        right: StructuredDocument | DocumentContent,
        *,
        enhanced: bool = False,
        tenant_id: str = "",
    ) -> DocumentComparisonResult | ComparisonResult:
        if isinstance(left, StructuredDocument) and isinstance(right, StructuredDocument):
            result = compare_structured(left, right)
        elif isinstance(left, DocumentContent) and isinstance(right, DocumentContent):
            result = compare_text_sections(
                left.text, right.text, left_ref=left.document_id, right_ref=right.document_id
            )
        else:
            raise DocumentError("comparison_failed")
        if tenant_id:
            self.observer.on_compared(tenant_id=tenant_id, unchanged=result.unchanged)
        if enhanced:
            return ComparisonResult.from_document_comparison(result)
        return result

    def reconcile(
        self,
        role_map: Mapping[str, StructuredDocument],
        profile: ReconciliationProfile,
        *,
        tenant_id: str = "",
    ) -> ReconciliationResult:
        result = reconcile_documents(role_map, profile)
        if tenant_id:
            self.observer.on_reconciled(tenant_id=tenant_id, status=result.status)
        return result

    def link(self, left: StructuredDocument, right: StructuredDocument) -> DocumentLinkResult:
        return link_documents(left, right)

    def generate(
        self,
        *,
        tenant_id: str,
        format: str,
        title: str,
        paragraphs: list[str],
        tables: list | None = None,
        headings: list[str] | None = None,
        template: DocumentTemplate | None = None,
        fields: dict | None = None,
        re_ingest: bool = False,
        scope: MemoryScope | None = None,
    ) -> GeneratedDocument:
        tid = require_tenant_id(tenant_id)
        fmt = format.lower()
        if fmt == "docx":
            generated = generate_docx(
                tenant_id=tid,
                title=title,
                paragraphs=paragraphs,
                tables=tables,
                headings=headings,
                template=template,
                fields=fields,
            )
        elif fmt == "pdf":
            generated = generate_pdf(
                tenant_id=tid,
                title=title,
                paragraphs=paragraphs,
                template=template,
                fields=fields,
            )
        elif fmt in {"txt", "md"}:
            generated = generate_txt(
                tenant_id=tid,
                title=title,
                paragraphs=paragraphs,
                template=template,
                fields=fields,
            )
        else:
            raise DocumentError(GENERATION_FAILED)
        self.observer.on_generated(tenant_id=tid, format=fmt)
        if re_ingest and self.documents is not None:
            sc = scope or self._scope(tid)
            ingest_req = DocumentIngestRequest(
                scope=sc,
                filename=generated.filename,
                content=generated.content,
                source_type=SOURCE_SYSTEM,
                source_id=f"generated:{generated.template_id}",
                media_type=generated.media_type,
                sensitivity=SENSITIVITY_INTERNAL,
            )
            row = self.documents.ingest(ingest_req)
            generated = replace(
                generated,
                document_id=row.document_id,
                provenance={**dict(generated.provenance), "re_ingested": True},
            )
        return generated

    def convert(
        self,
        *,
        tenant_id: str,
        source_media_type: str,
        target_format: str,
        text: str = "",
        title: str = "converted",
    ) -> GeneratedDocument:
        return convert_document(
            tenant_id=tenant_id,
            source_media_type=source_media_type,
            target_format=target_format,
            text=text,
            title=title,
        )

    def plan_workload(
        self,
        *,
        document_id: str,
        tenant_id: str,
        operations: Sequence[str],
        page_count: int | None = None,
        byte_size: int | None = None,
        bulk: bool = False,
        force_interactive_hint: bool = False,
        **kwargs,
    ) -> PlannedDocument:
        return plan_document_job(
            document_id=document_id,
            tenant_id=tenant_id,
            operations=operations,
            page_count=page_count,
            byte_size=byte_size,
            bulk=bulk,
            force_interactive_hint=force_interactive_hint,
            **kwargs,
        )

    def enqueue_metadata(self, planned: PlannedDocument) -> Mapping[str, object]:
        """Return trusted enqueue metadata for TaskQueue (caller stamps only these)."""
        return dict(planned.trusted_metadata)

    def process_pipeline(
        self,
        *,
        tenant_id: str,
        document_id: str | None = None,
        content: DocumentContent | None = None,
        text: str | None = None,
        filename: str = "",
        schema: ExtractionSchema | None = None,
        page_stats: Sequence[Mapping[str, object]] | None = None,
        job: DocumentProcessingJob | None = None,
        operations: Sequence[str] | None = None,
    ) -> DocumentResult:
        """ingest→ocr plan→classify→extract→validate with job checkpoints."""
        tid = require_tenant_id(tenant_id)
        ops = tuple(operations or ("ingest", "ocr", "classify", "extract", "validate"))
        doc_id = document_id or (content.document_id if content else new_id("doc-"))

        page_count: int | None = None
        byte_size: int | None = None
        if page_stats:
            page_count = len(page_stats)
        elif content is not None and content.pages:
            page_count = len(content.pages)

        if job is None:
            planned = plan_document_job(
                document_id=doc_id,
                tenant_id=tid,
                operations=ops,
                page_count=page_count,
                byte_size=byte_size,
            )
            job = planned.job
            if planned.enqueue and OP_OCR in ops:
                raise DocumentError(DOCUMENT_OCR_BATCH_REQUIRED)

        job = replace(job, status=JOB_RUNNING, started_at=job.started_at or utc_now())
        job = self._checkpoint(job, stage=STAGE_INGEST, status=JOB_RUNNING)
        self.observer.on_processing_started(job_id=job.job_id, tenant_id=tid, stage=STAGE_INGEST)

        warnings: list[str] = []
        errors: list[str] = []
        try:
            # Resolve content
            if content is None:
                if text is not None:
                    content = DocumentContent(document_id=doc_id, text=text)
                elif document_id:
                    content = self.extract_content(document_id, tenant_id=tid)
                else:
                    content = DocumentContent(document_id=doc_id, text="")

            self.observer.on_native_extracted(
                document_id=doc_id,
                tenant_id=tid,
                char_count=len(content.text or ""),
            )
            job = self._checkpoint(
                job,
                stage=STAGE_OCR,
                extra={"native_chars": len(content.text or "")},
            )

            ocr_decision = plan_ocr(
                native_text=content.text,
                page_stats=page_stats,
                provider=getattr(self.ocr, "name", "") or "",
                provider_available=bool(getattr(self.ocr, "available", False)),
            )
            self.observer.on_ocr(
                status=ocr_decision.status,
                document_id=doc_id,
                tenant_id=tid,
                page_count=ocr_decision.page_count,
            )
            if ocr_decision.status == OCR_REQUIRED and not getattr(self.ocr, "available", False):
                warnings.append("ocr_required_but_unavailable")

            job = self._checkpoint(
                job,
                stage=STAGE_CLASSIFY,
                extra={"ocr_status": ocr_decision.status},
            )
            classification = classify_document(content.text, filename=filename)
            self.observer.on_classified(
                document_id=doc_id, tenant_id=tid, doc_class=classification.doc_class
            )

            job = self._checkpoint(
                job,
                stage=STAGE_EXTRACT,
                extra={"doc_class": classification.doc_class},
            )
            if schema is not None:
                structured = extract_structured_with_schema(content, schema)
            else:
                doc_type = (
                    classification.doc_class
                    if classification.doc_class not in {"unknown", ""}
                    else None
                )
                structured = extract_structured(
                    content, document_type=doc_type, filename=filename
                )
            field_count = len(structured.field_evidence)
            self.observer.on_extracted(
                document_id=doc_id, tenant_id=tid, field_count=field_count
            )

            job = self._checkpoint(job, stage=STAGE_VALIDATE)
            vr = validate_structured(structured)
            self.observer.on_validated(document_id=doc_id, tenant_id=tid, ok=vr.ok)
            if not vr.ok:
                errors.extend(vr.errors)
            warnings.extend(vr.warnings)

            store = self.store
            version_id = job.version_id or new_id("ver-")
            if store is not None and hasattr(store, "save_document_version"):
                store.save_document_version(
                    DocumentVersion(
                        document_id=doc_id,
                        version_id=version_id,
                        artifact_id=doc_id,
                        content_hash="",
                        transformation_reason="process_pipeline",
                        producing_operation="extract",
                        producing_tool_or_model="document_intelligence",
                    )
                )

            job = self._checkpoint(
                job,
                stage=STAGE_DONE,
                status=JOB_COMPLETED,
                extra={"version_id": version_id},
            )
            job = replace(job, completed_at=utc_now(), version_id=version_id)
            self._save_job(job)

            return DocumentResult(
                document_id=doc_id,
                version_id=version_id,
                status=JOB_COMPLETED,
                text_ref=f"doc:{doc_id}:text",
                classification={
                    "doc_class": classification.doc_class,
                    "status": classification.status,
                    "confidence": classification.confidence,
                    "classifier_version": classification.classifier_version,
                },
                fields={
                    **dict(structured.fields),
                    **dict(structured.identifiers),
                    **dict(structured.amounts),
                    **dict(structured.dates),
                },
                validation={"ok": vr.ok, "errors": list(vr.errors)},
                provenance={
                    "ocr_status": ocr_decision.status,
                    "job_id": job.job_id,
                },
                warnings=tuple(warnings),
                errors=tuple(errors),
            )
        except DocumentError as exc:
            job = self._checkpoint(
                job,
                stage=job.stage,
                status=JOB_FAILED,
                extra={"error": exc.reason},
            )
            self.observer.on_failed(tenant_id=tid, reason=exc.reason, job_id=job.job_id)
            raise
        except Exception as exc:
            job = self._checkpoint(
                job,
                stage=job.stage,
                status=JOB_FAILED,
                extra={"error": "pipeline_failed"},
            )
            self.observer.on_failed(tenant_id=tid, reason="pipeline_failed", job_id=job.job_id)
            raise DocumentError("document_parse_failed") from exc

    def plan_large_extraction(
        self,
        *,
        document_id: str,
        tenant_id: str,
        size_bytes: int = 0,
        page_count: int = 0,
        text_chars: int = 0,
        enqueue: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        async_needed = self.large_policy.requires_async(
            size_bytes=size_bytes, page_count=page_count or None, text_chars=text_chars
        )
        if not async_needed:
            return {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "async": False,
                "batches": [],
                "status": "sync",
            }
        plan = build_large_doc_plan(
            document_id=document_id,
            tenant_id=tenant_id,
            page_count=max(1, page_count),
            batch_size=self.large_policy.pages_per_batch,
            document_type=str((metadata or {}).get("document_type") or "pdf"),
            text_chars=text_chars,
            max_text_chars_per_batch=self.large_policy.max_text_chars_per_batch,
        )
        plan["async"] = True
        plan["execution_key"] = large_extract_execution_key(tenant_id, document_id)
        plan["status"] = "planned"
        if enqueue:
            plan.update(self.enqueue_large_extraction(plan, metadata=metadata))
        return plan

    def enqueue_large_extraction(self, plan: dict, *, metadata: dict | None = None) -> dict:
        from documents.errors import LARGE_DOCUMENT_WORKFLOW_UNAVAILABLE

        if self.workflow_runtime is None:
            raise DocumentError(LARGE_DOCUMENT_WORKFLOW_UNAVAILABLE)
        tenant_id = str(plan.get("tenant_id") or "legacy-default")
        document_id = str(plan.get("document_id") or "")
        tenant = normalize_tenant_id(tenant_id)
        execution_key = str(
            plan.get("execution_key")
            or large_extract_execution_key(tenant_id, document_id)
        )
        existing = self.workflow_runtime.state_manager.find_by_execution_key(
            execution_key, tenant_id=tenant
        )
        if existing is not None:
            enq = self.workflow_runtime.enqueue_existing(existing.workflow_id, idempotent=True)
            return {
                "status": enq.get("status") or existing.status,
                "workflow_id": existing.workflow_id,
                "execution_key": execution_key,
                "idempotent": True,
                "queue_task_id": enq.get("queue_task_id"),
            }
        meta = {
            "document_id": document_id,
            "tenant_id": tenant,
            "page_count": int(plan.get("page_count") or 1),
            "batch_count": int(plan.get("batch_count") or 0),
            "batches": list(plan.get("batches") or ()),
            **dict(metadata or {}),
        }
        created = self.workflow_runtime.create_workflow(
            "document.large_extract",
            "1",
            execution_key=execution_key,
            metadata=meta,
            tenant_id=tenant,
        )
        enq = self.workflow_runtime.enqueue_existing(created["workflow_id"])
        return {
            "status": enq.get("status") or "queued",
            "workflow_id": created["workflow_id"],
            "execution_key": execution_key,
            "idempotent": False,
            "queue_task_id": enq.get("queue_task_id"),
        }

    def to_acquisition_artifact_text(self, structured: StructuredDocument) -> tuple[str, str, dict]:
        """Bridge helper — CSV-like text for Acquisition price/supplier parsers."""
        if structured.document_type == "price_list" and structured.line_items:
            lines = ["sku,ean,name,price,currency,stock"]
            currency = str(dict(structured.amounts).get("currency") or "USD")
            for item in structured.line_items:
                lines.append(
                    ",".join(
                        [
                            str(item.get("sku") or ""),
                            str(item.get("ean") or ""),
                            str(item.get("name") or "").replace(",", " "),
                            str(item.get("price") or ""),
                            currency,
                            str(item.get("stock") or ""),
                        ]
                    )
                )
            return "\n".join(lines), "text/csv", {"record_hint": "supplier_item"}
        import json

        payload = {
            "document_type": structured.document_type,
            "fields": dict(structured.fields),
            "identifiers": dict(structured.identifiers),
            "amounts": dict(structured.amounts),
            "line_items": list(structured.line_items),
        }
        return json.dumps(payload, ensure_ascii=False), "application/json", {
            "record_hint": structured.document_type
        }


def build_document_intelligence(
    *,
    document_service=None,
    env: dict | None = None,
    ocr_provider=None,
    rasterizer=None,
    workflow_runtime=None,
    large_policy: LargeDocumentPolicy | None = None,
    observer=None,
) -> DocumentIntelligenceService:
    ocr = ocr_provider if ocr_provider is not None else build_ocr_provider(env)
    rast = rasterizer if rasterizer is not None else build_pdf_rasterizer(env)
    return DocumentIntelligenceService(
        document_service,
        ocr_provider=ocr,
        rasterizer=rast,
        large_policy=large_policy,
        workflow_runtime=workflow_runtime,
        observer=observer,
    )
