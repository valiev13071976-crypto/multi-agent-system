"""PDF parser — text-based extraction only; no OCR."""

from __future__ import annotations

import io
import uuid

from documents.errors import (
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_REQUIRES_OCR,
    DOCUMENT_TOO_LARGE,
    DOCUMENT_TOO_MANY_PAGES,
    DocumentError,
)
from documents.models import DOC_PDF, ParsedDocument, TextBlock, content_hash_text


class PdfDocumentParser:
    parser_id = "pdf_v1"
    version = "1.0.0"
    supported_types = (DOC_PDF,)

    def parse(self, *, document_id: str, data: bytes, filename: str, limits: dict) -> ParsedDocument:
        max_pages = int(limits.get("max_pages", 200))
        max_text = int(limits.get("max_text_bytes", 1_000_000))
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise DocumentError(DOCUMENT_PARSE_FAILED) from exc
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
        except Exception as exc:
            from documents.errors import DOCUMENT_MALFORMED

            raise DocumentError(DOCUMENT_MALFORMED) from exc

        if getattr(reader, "is_encrypted", False):
            from documents.errors import DOCUMENT_ENCRYPTED

            # Try empty password; still treat unresolved encryption as encrypted
            try:
                ok = reader.decrypt("")  # type: ignore[attr-defined]
                if not ok:
                    raise DocumentError(DOCUMENT_ENCRYPTED)
            except DocumentError:
                raise
            except Exception as exc:
                raise DocumentError(DOCUMENT_ENCRYPTED) from exc

        page_count = len(reader.pages)
        if page_count > max_pages:
            raise DocumentError(DOCUMENT_TOO_MANY_PAGES)

        blocks = []
        total = 0
        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            text = text.strip()
            if not text:
                continue
            encoded = text.encode("utf-8")
            total += len(encoded)
            if total > max_text:
                raise DocumentError(DOCUMENT_TOO_LARGE)
            blocks.append(
                TextBlock(
                    block_id=str(uuid.uuid4()),
                    ordinal=len(blocks),
                    text=text,
                    content_hash=content_hash_text(text),
                    source_location=f"pdf:page:{i + 1}",
                    page=i + 1,
                )
            )

        if page_count > 0 and not blocks:
            raise DocumentError(DOCUMENT_REQUIRES_OCR)

        return ParsedDocument(
            document_id=document_id,
            text_blocks=tuple(blocks),
            tables=(),
            metadata_safe={"filename": filename, "page_count": page_count},
            parser_id=self.parser_id,
            parser_version=self.version,
            title=filename,
            pages=page_count,
        )
