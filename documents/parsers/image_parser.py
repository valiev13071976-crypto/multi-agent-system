"""Image document parser — requires OCR provider; otherwise structured requires_ocr."""

from __future__ import annotations

import uuid

from documents.errors import DOCUMENT_REQUIRES_OCR, DocumentError
from documents.models import DOC_IMAGE, ParsedDocument, TextBlock, content_hash_text


class ImageDocumentParser:
    parser_id = "image_v1"
    version = "1.0.0"
    supported_types = (DOC_IMAGE,)

    def __init__(self, ocr_provider=None):
        self._ocr = ocr_provider

    def parse(self, *, document_id: str, data: bytes, filename: str, limits: dict) -> ParsedDocument:
        if self._ocr is None or not getattr(self._ocr, "available", False):
            raise DocumentError(DOCUMENT_REQUIRES_OCR)
        result = self._ocr.recognize(data, filename=filename)
        text = str(result.get("text") or "").strip()
        if not text:
            raise DocumentError(DOCUMENT_REQUIRES_OCR)
        block = TextBlock(
            block_id=str(uuid.uuid4()),
            ordinal=0,
            text=text[: int(limits.get("max_text_bytes", 1_000_000))],
            content_hash=content_hash_text(text),
            source_location="image:ocr:page:1",
            page=1,
            metadata_safe={
                "ocr_provider": result.get("provider"),
                "ocr_confidence": result.get("confidence_level"),
            },
        )
        return ParsedDocument(
            document_id=document_id,
            text_blocks=(block,),
            tables=(),
            metadata_safe={
                "filename": filename,
                "extraction_method": "ocr",
                "ocr_provider": result.get("provider"),
                "bytes": len(data),
            },
            parser_id=self.parser_id,
            parser_version=self.version,
            title=filename,
            pages=1,
            warnings=tuple(result.get("warnings") or ()),
        )
