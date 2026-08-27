"""Document Intelligence Service — extract/OCR/compare/generate/convert."""

from __future__ import annotations

import io
from dataclasses import replace

from documents.errors import (
    DOCUMENT_ACCESS_DENIED,
    DOCUMENT_REQUIRES_OCR,
    OCR_UNAVAILABLE,
    DocumentError,
)
from documents.intelligence.classify import classify_document_text
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
from documents.intelligence.extraction import extract_structured
from documents.intelligence.generate import generate_docx, generate_pdf, generate_txt
from documents.intelligence.large import LargeDocumentPolicy, build_large_doc_plan, large_extract_execution_key
from documents.intelligence.linking import link_documents
from documents.intelligence.ocr import NullOCRProvider, build_ocr_provider
from documents.intelligence.pdf_ocr import (
    build_pdf_document_content,
    content_to_parsed_document,
    peek_pdf_page_count,
)
from documents.intelligence.raster import NullPdfRasterizer, build_pdf_rasterizer
from documents.models import DOC_PDF, ParsedDocument
from documents.type_detect import resolve_document_type
from memory.models import MemoryScope
from security.tenant import normalize_tenant_id, tenants_match


class DocumentIntelligenceService:
    def __init__(
        self,
        document_service=None,
        *,
        ocr_provider=None,
        rasterizer=None,
        large_policy: LargeDocumentPolicy | None = None,
        workflow_runtime=None,
    ):
        self.documents = document_service
        self.ocr = ocr_provider if ocr_provider is not None else NullOCRProvider()
        self.rasterizer = rasterizer if rasterizer is not None else NullPdfRasterizer()
        self.large_policy = large_policy or LargeDocumentPolicy()
        self.workflow_runtime = workflow_runtime
        self._structured_cache: dict[tuple[str, str], StructuredDocument] = {}
        self._content_cache: dict[tuple[str, str], DocumentContent] = {}

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
            # Rebuild from chunks when full ParsedDocument not available
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
    ) -> StructuredDocument:
        content = content or self.extract_content(document_id, tenant_id=tenant_id)
        structured = extract_structured(
            content, document_type=document_type, filename=filename
        )
        self._structured_cache[(normalize_tenant_id(tenant_id), document_id)] = structured
        return structured

    def classify(self, text: str, *, filename: str = "") -> tuple[str, str, tuple[str, ...]]:
        return classify_document_text(text, filename=filename)

    def compare(
        self,
        left: StructuredDocument | DocumentContent,
        right: StructuredDocument | DocumentContent,
    ) -> DocumentComparisonResult:
        if isinstance(left, StructuredDocument) and isinstance(right, StructuredDocument):
            return compare_structured(left, right)
        if isinstance(left, DocumentContent) and isinstance(right, DocumentContent):
            return compare_text_sections(
                left.text, right.text, left_ref=left.document_id, right_ref=right.document_id
            )
        raise DocumentError("comparison_failed")

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
    ) -> GeneratedDocument:
        fmt = format.lower()
        if fmt == "docx":
            return generate_docx(
                tenant_id=tenant_id,
                title=title,
                paragraphs=paragraphs,
                tables=tables,
                headings=headings,
            )
        if fmt == "pdf":
            return generate_pdf(tenant_id=tenant_id, title=title, paragraphs=paragraphs)
        if fmt in {"txt", "md"}:
            return generate_txt(tenant_id=tenant_id, title=title, paragraphs=paragraphs)
        raise DocumentError("generation_failed")

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
) -> DocumentIntelligenceService:
    ocr = ocr_provider if ocr_provider is not None else build_ocr_provider(env)
    rast = rasterizer if rasterizer is not None else build_pdf_rasterizer(env)
    return DocumentIntelligenceService(
        document_service,
        ocr_provider=ocr,
        rasterizer=rast,
        large_policy=large_policy,
        workflow_runtime=workflow_runtime,
    )
