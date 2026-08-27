"""Document Intelligence Service — extract/OCR/compare/generate/convert."""

from __future__ import annotations

import io
from dataclasses import replace

from documents.errors import (
    DOCUMENT_ACCESS_DENIED,
    DOCUMENT_REQUIRES_OCR,
    DOCUMENT_TOO_LARGE,
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
from documents.intelligence.large import LargeDocumentPolicy, build_large_doc_plan
from documents.intelligence.linking import link_documents
from documents.intelligence.ocr import NullOCRProvider, build_ocr_provider
from documents.models import DOC_IMAGE, DOC_PDF, ParsedDocument, content_hash_bytes
from documents.type_detect import resolve_document_type
from memory.models import MemoryScope
from security.tenant import normalize_tenant_id, tenants_match


class DocumentIntelligenceService:
    def __init__(
        self,
        document_service=None,
        *,
        ocr_provider=None,
        large_policy: LargeDocumentPolicy | None = None,
    ):
        self.documents = document_service
        self.ocr = ocr_provider if ocr_provider is not None else NullOCRProvider()
        self.large_policy = large_policy or LargeDocumentPolicy()
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
        """Text PDF via parser; scanned → OCR when available."""
        from documents.parsers.pdf import PdfDocumentParser

        limits = limits or {}
        try:
            parsed = PdfDocumentParser().parse(
                document_id=document_id, data=data, filename=filename, limits=limits
            )
            return content_from_parsed(parsed, extraction_method="pdf_text")
        except DocumentError as exc:
            if exc.reason != DOCUMENT_REQUIRES_OCR:
                raise
            if not getattr(self.ocr, "available", False):
                raise
            # Page-level OCR using pypdf images is limited; treat whole PDF bytes via provider if images
            # For foundation: OCR unavailable for raw PDF bytes without rasterization → try provider
            try:
                return self.ocr_document(
                    document_id, tenant_id=tenant_id, data=data, filename=filename
                )
            except DocumentError:
                # Explicit: scanned PDF needs raster OCR backend
                raise DocumentError(OCR_UNAVAILABLE)

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
            }
        plan = build_large_doc_plan(
            document_id=document_id,
            tenant_id=tenant_id,
            page_count=max(1, page_count),
        )
        plan["async"] = True
        return plan

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
        # Generic JSON dump of structured fields
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
) -> DocumentIntelligenceService:
    ocr = ocr_provider if ocr_provider is not None else build_ocr_provider(env)
    return DocumentIntelligenceService(document_service, ocr_provider=ocr)
