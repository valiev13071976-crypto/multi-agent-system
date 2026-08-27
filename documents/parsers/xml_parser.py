"""Safe XML document parser — rejects entity expansion / DOCTYPE."""

from __future__ import annotations

import io
import re
import uuid
from xml.etree import ElementTree

from documents.errors import DOCUMENT_MALFORMED, DOCUMENT_TOO_LARGE, DocumentError
from documents.models import DOC_XML, ParsedDocument, TextBlock, content_hash_text


_FORBIDDEN = re.compile(r"<!DOCTYPE|<!ENTITY|SYSTEM\s+[\"']|PUBLIC\s+[\"']", re.I)


class XmlDocumentParser:
    parser_id = "xml_v1"
    version = "1.0.0"
    supported_types = (DOC_XML,)

    def parse(self, *, document_id: str, data: bytes, filename: str, limits: dict) -> ParsedDocument:
        max_text = int(limits.get("max_text_bytes", 1_000_000))
        if len(data) > max_text:
            raise DocumentError(DOCUMENT_TOO_LARGE)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentError(DOCUMENT_MALFORMED) from exc
        if _FORBIDDEN.search(text):
            raise DocumentError(DOCUMENT_MALFORMED)
        try:
            # Default ElementTree does not resolve external entities in modern CPython
            # when using fromstring on a string without a custom parser that enables them.
            root = ElementTree.fromstring(text)
        except Exception as exc:
            raise DocumentError(DOCUMENT_MALFORMED) from exc

        parts = []
        for i, el in enumerate(root.iter()):
            if i > 10_000:
                raise DocumentError(DOCUMENT_TOO_LARGE)
            if el.text and el.text.strip():
                parts.append(f"{el.tag}: {el.text.strip()}")
        body = "\n".join(parts)[:max_text]
        block = TextBlock(
            block_id=str(uuid.uuid4()),
            ordinal=0,
            text=body or root.tag,
            content_hash=content_hash_text(body or root.tag),
            source_location=f"xml:{root.tag}",
        )
        return ParsedDocument(
            document_id=document_id,
            text_blocks=(block,),
            tables=(),
            metadata_safe={"filename": filename, "root_tag": root.tag},
            parser_id=self.parser_id,
            parser_version=self.version,
            title=filename,
        )
