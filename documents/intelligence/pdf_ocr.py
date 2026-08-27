"""PDF OCR path: text layer + rasterize scanned pages + OCR merge."""

from __future__ import annotations

import io
import uuid

from documents.errors import (
    DOCUMENT_ENCRYPTED,
    DOCUMENT_MALFORMED,
    DOCUMENT_REQUIRES_OCR,
    DOCUMENT_TOO_LARGE,
    DOCUMENT_TOO_MANY_PAGES,
    OCR_FAILED,
    OCR_UNAVAILABLE,
    PDF_RASTERIZATION_UNAVAILABLE,
    DocumentError,
)
from documents.intelligence.contracts import CONF_HIGH, CONF_MEDIUM, DocumentContent
from documents.intelligence.ocr import NullOCRProvider
from documents.intelligence.raster import NullPdfRasterizer
from documents.models import ParsedDocument, TextBlock, content_hash_text


def _open_pdf_reader(data: bytes):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentError(DOCUMENT_MALFORMED) from exc
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except Exception as exc:
        raise DocumentError(DOCUMENT_MALFORMED) from exc
    if getattr(reader, "is_encrypted", False):
        try:
            ok = reader.decrypt("")  # type: ignore[attr-defined]
            if not ok:
                raise DocumentError(DOCUMENT_ENCRYPTED)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError(DOCUMENT_ENCRYPTED) from exc
    return reader


def peek_pdf_page_count(data: bytes) -> int:
    reader = _open_pdf_reader(data)
    return len(reader.pages)


def extract_pdf_pages_text(data: bytes, *, max_pages: int, max_text: int) -> tuple[dict[int, str], int]:
    """Return ({page_num: text}, page_count). Empty string means OCR needed."""
    reader = _open_pdf_reader(data)
    page_count = len(reader.pages)
    if page_count > max_pages:
        raise DocumentError(DOCUMENT_TOO_MANY_PAGES)
    pages: dict[int, str] = {}
    total = 0
    for i, page in enumerate(reader.pages):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        encoded = text.encode("utf-8")
        total += len(encoded)
        if total > max_text:
            raise DocumentError(DOCUMENT_TOO_LARGE)
        pages[i + 1] = text
    return pages, page_count


def build_pdf_document_content(
    *,
    document_id: str,
    data: bytes,
    filename: str,
    ocr_provider,
    rasterizer,
    limits: dict | None = None,
) -> DocumentContent:
    """
    Text PDF pages keep text layer; empty pages → rasterize → OCR.
    Never sends raw PDF bytes to image-only OCR.
    """
    limits = limits or {}
    max_pages = int(limits.get("max_pages", 200))
    max_text = int(limits.get("max_text_bytes", 1_000_000))
    page_texts, page_count = extract_pdf_pages_text(data, max_pages=max_pages, max_text=max_text)

    ocr_needed = [p for p, t in page_texts.items() if not t]
    text_pages = [p for p, t in page_texts.items() if t]
    warnings: list[str] = []
    methods = set()
    confidences = []

    if not ocr_needed and not text_pages and page_count > 0:
        # all empty already captured in ocr_needed
        pass

    if ocr_needed:
        if ocr_provider is None or not getattr(ocr_provider, "available", False):
            if not text_pages:
                raise DocumentError(DOCUMENT_REQUIRES_OCR)
            raise DocumentError(OCR_UNAVAILABLE)
        if rasterizer is None or not getattr(rasterizer, "available", False):
            if not text_pages:
                # Fully scanned and cannot rasterize
                raise DocumentError(PDF_RASTERIZATION_UNAVAILABLE)
            raise DocumentError(PDF_RASTERIZATION_UNAVAILABLE)
        try:
            rasters = rasterizer.rasterize(data, pages=tuple(ocr_needed))
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError(PDF_RASTERIZATION_UNAVAILABLE) from exc
        by_page = {r.page: r for r in rasters}
        for p in ocr_needed:
            raster = by_page.get(p)
            if raster is None:
                raise DocumentError(PDF_RASTERIZATION_UNAVAILABLE)
            try:
                result = ocr_provider.recognize(
                    raster.image_bytes, filename=f"{filename}:page-{p}.png"
                )
            except DocumentError:
                raise
            except Exception as exc:
                raise DocumentError(OCR_FAILED) from exc
            text = str(result.get("text") or "").strip()
            page_texts[p] = text
            methods.add("ocr")
            confidences.append(str(result.get("confidence_level") or CONF_MEDIUM))
            warnings.extend(list(result.get("warnings") or ()))
            if not text:
                warnings.append(f"ocr_empty_page:{p}")

    if text_pages:
        methods.add("pdf_text")

    if page_count > 0 and not any(page_texts.values()):
        raise DocumentError(DOCUMENT_REQUIRES_OCR)

    pages_meta = []
    sections = []
    ordered_parts = []
    for p in range(1, page_count + 1):
        text = page_texts.get(p) or ""
        method = "ocr" if p in ocr_needed else "pdf_text"
        loc = f"pdf:page:{p}" if method == "pdf_text" else f"pdf:ocr:page:{p}"
        pages_meta.append(
            {
                "page": p,
                "source_location": loc,
                "extraction_method": method,
                "text": text,
                "text_preview": text[:500],
                "content_hash": content_hash_text(text) if text else "",
            }
        )
        if text:
            ordered_parts.append(text)
            sections.append({"section": f"page:{p}", "source_location": loc})

    if ocr_needed and text_pages:
        extraction_method = "pdf_mixed_ocr"
    elif ocr_needed:
        extraction_method = "pdf_ocr"
    else:
        extraction_method = "pdf_text"

    conf = CONF_HIGH
    if ocr_needed:
        conf = CONF_MEDIUM
    if confidences and all(c == "low" for c in confidences):
        conf = "low"

    return DocumentContent(
        document_id=document_id,
        text="\n\n".join(ordered_parts),
        pages=tuple(pages_meta),
        sections=tuple(sections),
        tables=(),
        metadata={
            "filename": filename,
            "page_count": page_count,
            "ocr_pages": list(ocr_needed),
            "text_pages": list(text_pages),
            "methods": sorted(methods),
        },
        extraction_method=extraction_method,
        confidence=conf,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def content_to_parsed_document(content: DocumentContent, *, parser_id: str = "pdf_ocr_v1") -> ParsedDocument:
    blocks = []
    for i, page in enumerate(content.pages):
        text = str(page.get("text") or page.get("text_preview") or "")
        loc = str(page.get("source_location") or f"pdf:page:{i + 1}")
        page_no = int(page.get("page") or (i + 1))
        if not text:
            continue
        blocks.append(
            TextBlock(
                block_id=str(uuid.uuid4()),
                ordinal=len(blocks),
                text=text,
                content_hash=content_hash_text(text),
                source_location=loc,
                page=page_no,
                metadata_safe={
                    "extraction_method": page.get("extraction_method") or content.extraction_method,
                },
            )
        )
    if not blocks and content.text:
        blocks.append(
            TextBlock(
                block_id=str(uuid.uuid4()),
                ordinal=0,
                text=content.text,
                content_hash=content_hash_text(content.text),
                source_location="pdf:merged",
            )
        )
    return ParsedDocument(
        document_id=content.document_id,
        text_blocks=tuple(blocks),
        tables=(),
        metadata_safe={k: v for k, v in dict(content.metadata).items() if k != "raw"},
        parser_id=parser_id,
        parser_version="1.1.0",
        title=str(content.metadata.get("filename") or ""),
        pages=int(content.metadata.get("page_count") or len(blocks) or 0),
        warnings=tuple(content.warnings),
        partial=bool(content.warnings),
    )
