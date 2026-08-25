"""Markdown parser — headings/sections preserved; no HTML/JS render."""

from __future__ import annotations

import re
import uuid

from documents.errors import DOCUMENT_PARSE_FAILED, DOCUMENT_TOO_LARGE, DocumentError
from documents.models import DOC_MD, ParsedDocument, TextBlock, content_hash_text


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


class MarkdownDocumentParser:
    parser_id = "md_v1"
    version = "1.0.0"
    supported_types = (DOC_MD,)

    def parse(self, *, document_id: str, data: bytes, filename: str, limits: dict) -> ParsedDocument:
        max_text = int(limits.get("max_text_bytes", 1_000_000))
        if len(data) > max_text:
            raise DocumentError(DOCUMENT_TOO_LARGE)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentError(DOCUMENT_PARSE_FAILED) from exc
        # Strip fenced HTML-ish tags without executing
        text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
        blocks = []
        section = None
        buf: list[str] = []
        ordinal = 0

        def flush():
            nonlocal ordinal, buf
            body = "\n".join(buf).strip()
            buf = []
            if not body:
                return
            blocks.append(
                TextBlock(
                    block_id=str(uuid.uuid4()),
                    ordinal=ordinal,
                    text=body[:max_text],
                    content_hash=content_hash_text(body),
                    source_location=f"md:section:{ordinal}",
                    section=section,
                )
            )
            ordinal += 1

        for line in text.splitlines():
            m = _HEADING_RE.match(line.strip())
            if m:
                flush()
                section = m.group(2).strip()
                blocks.append(
                    TextBlock(
                        block_id=str(uuid.uuid4()),
                        ordinal=ordinal,
                        text=section[:max_text],
                        content_hash=content_hash_text(section),
                        source_location=f"md:heading:{ordinal}",
                        section=section,
                        metadata_safe={"heading_level": len(m.group(1))},
                    )
                )
                ordinal += 1
            else:
                buf.append(line)
        flush()
        return ParsedDocument(
            document_id=document_id,
            text_blocks=tuple(blocks),
            tables=(),
            metadata_safe={"filename": filename},
            parser_id=self.parser_id,
            parser_version=self.version,
            title=filename,
        )
