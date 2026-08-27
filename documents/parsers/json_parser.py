"""JSON document parser."""

from __future__ import annotations

import json
import uuid

from documents.errors import DOCUMENT_MALFORMED, DOCUMENT_TOO_LARGE, DocumentError
from documents.models import DOC_JSON, ParsedDocument, TextBlock, content_hash_text


class JsonDocumentParser:
    parser_id = "json_v1"
    version = "1.0.0"
    supported_types = (DOC_JSON,)

    def parse(self, *, document_id: str, data: bytes, filename: str, limits: dict) -> ParsedDocument:
        max_text = int(limits.get("max_text_bytes", 1_000_000))
        max_depth = int(limits.get("max_json_depth", 32))
        if len(data) > max_text:
            raise DocumentError(DOCUMENT_TOO_LARGE)
        try:
            text = data.decode("utf-8")
            payload = json.loads(text)
        except Exception as exc:
            raise DocumentError(DOCUMENT_MALFORMED) from exc

        def _depth(node, d=0) -> int:
            if d > max_depth:
                raise DocumentError(DOCUMENT_TOO_LARGE)
            if isinstance(node, dict):
                return max([_depth(v, d + 1) for v in node.values()] or [d])
            if isinstance(node, list):
                return max([_depth(v, d + 1) for v in node] or [d])
            return d

        try:
            depth = _depth(payload)
        except DocumentError:
            raise
        pretty = json.dumps(payload, ensure_ascii=False, indent=2, default=str)[:max_text]
        block = TextBlock(
            block_id=str(uuid.uuid4()),
            ordinal=0,
            text=pretty,
            content_hash=content_hash_text(pretty),
            source_location="json:root",
        )
        return ParsedDocument(
            document_id=document_id,
            text_blocks=(block,),
            tables=(),
            metadata_safe={
                "filename": filename,
                "json_depth": depth,
                "root_type": type(payload).__name__,
            },
            parser_id=self.parser_id,
            parser_version=self.version,
            title=filename,
        )
