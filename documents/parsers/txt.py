"""Plain text parser."""

from __future__ import annotations

import uuid

from documents.errors import DOCUMENT_PARSE_FAILED, DOCUMENT_TOO_LARGE, DocumentError
from documents.models import DOC_TXT, ParsedDocument, TextBlock, content_hash_text


class TxtDocumentParser:
    parser_id = "txt_v1"
    version = "1.0.0"
    supported_types = (DOC_TXT,)

    def parse(self, *, document_id: str, data: bytes, filename: str, limits: dict) -> ParsedDocument:
        max_text = int(limits.get("max_text_bytes", 1_000_000))
        if len(data) > max_text:
            raise DocumentError(DOCUMENT_TOO_LARGE)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise DocumentError(DOCUMENT_PARSE_FAILED) from exc
        if len(text.encode("utf-8")) > max_text:
            raise DocumentError(DOCUMENT_TOO_LARGE)
        blocks = []
        for i, para in enumerate(p for p in text.split("\n\n") if p.strip()):
            clipped = para.strip()[: max(1, max_text)]
            blocks.append(
                TextBlock(
                    block_id=str(uuid.uuid4()),
                    ordinal=i,
                    text=clipped,
                    content_hash=content_hash_text(clipped),
                    source_location=f"txt:para:{i}",
                )
            )
        if not blocks and text.strip():
            clipped = text.strip()[:max_text]
            blocks.append(
                TextBlock(
                    block_id=str(uuid.uuid4()),
                    ordinal=0,
                    text=clipped,
                    content_hash=content_hash_text(clipped),
                    source_location="txt:body",
                )
            )
        return ParsedDocument(
            document_id=document_id,
            text_blocks=tuple(blocks),
            tables=(),
            metadata_safe={"filename": filename, "encoding": "utf-8"},
            parser_id=self.parser_id,
            parser_version=self.version,
            title=filename,
        )
