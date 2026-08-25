"""Document chunker — heading/page/sheet aware, bounded overlap."""

from __future__ import annotations

import uuid

from documents.errors import DOCUMENT_TOO_MANY_CHUNKS, DocumentError
from documents.models import (
    DOCUMENT_CHUNKER_VERSION,
    DocumentChunkRecord,
    ParsedDocument,
    content_hash_text,
)
from memory.models import MemoryScope, utc_now
from security.encryption import SENSITIVITY_INTERNAL


class DocumentChunker:
    version = DOCUMENT_CHUNKER_VERSION

    def __init__(
        self,
        *,
        max_chars: int = 2_000,
        overlap_chars: int = 100,
        max_chunks: int = 500,
    ):
        self.max_chars = int(max_chars)
        self.overlap_chars = min(int(overlap_chars), max(0, self.max_chars // 4))
        self.max_chunks = int(max_chunks)

    def chunk(
        self,
        parsed: ParsedDocument,
        *,
        scope: MemoryScope,
        sensitivity: str = SENSITIVITY_INTERNAL,
        provenance: dict | None = None,
    ) -> tuple[DocumentChunkRecord, ...]:
        pieces: list[tuple[str, str, dict]] = []
        for block in parsed.text_blocks:
            pieces.append(
                (
                    block.text,
                    block.source_location,
                    {"section": block.section, "page": block.page, "kind": "text"},
                )
            )
        for table in parsed.tables:
            header = " | ".join(table.columns)
            lines = [header] + [" | ".join(r) for r in table.rows[:50]]
            pieces.append(
                (
                    "\n".join(lines),
                    table.source_location,
                    {"kind": "table", "table_name": table.name},
                )
            )

        chunks: list[DocumentChunkRecord] = []
        ordinal = 0
        for text, location, meta in pieces:
            for part, loc_suffix in self._split(text):
                if ordinal >= self.max_chunks:
                    raise DocumentError(DOCUMENT_TOO_MANY_CHUNKS)
                chunks.append(
                    DocumentChunkRecord(
                        chunk_id=str(uuid.uuid4()),
                        document_id=parsed.document_id,
                        scope=scope,
                        ordinal=ordinal,
                        content_hash=content_hash_text(part),
                        source_location=f"{location}{loc_suffix}",
                        content_safe=part,
                        sensitivity=sensitivity,
                        provenance_json=dict(provenance or {}),
                        metadata_safe=meta,
                        created_at=utc_now(),
                    )
                )
                ordinal += 1
        return tuple(chunks)

    def _split(self, text: str) -> list[tuple[str, str]]:
        text = str(text or "").strip()
        if not text:
            return []
        max_c = self.max_chars
        if len(text) <= max_c:
            return [(text, "")]
        out = []
        start = 0
        part_i = 0
        while start < len(text):
            end = min(len(text), start + max_c)
            # Prefer break at newline/space
            if end < len(text):
                window = text[start:end]
                br = max(window.rfind("\n"), window.rfind(" "))
                if br > max_c // 4:
                    end = start + br
            piece = text[start:end].strip()
            if piece:
                out.append((piece, f":part:{part_i}"))
                part_i += 1
            if end >= len(text):
                break
            start = max(end - self.overlap_chars, start + 1)
        return out


def chunker_policy_snapshot() -> dict:
    return {
        "document_chunker_version": DOCUMENT_CHUNKER_VERSION,
        "strategy": ["text_blocks", "tables", "sheet_previews", "bounded_overlap"],
        "formula_execution": False,
    }
