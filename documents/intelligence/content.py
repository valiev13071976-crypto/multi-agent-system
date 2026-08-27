"""Build DocumentContent from ParsedDocument / OCR results."""

from __future__ import annotations

from documents.intelligence.contracts import CONF_HIGH, CONF_LOW, CONF_MEDIUM, DocumentContent
from documents.models import ParsedDocument


def content_from_parsed(
    parsed: ParsedDocument,
    *,
    extraction_method: str = "parser",
    confidence: str | None = None,
) -> DocumentContent:
    text = "\n\n".join(b.text for b in parsed.text_blocks)
    pages = []
    for b in parsed.text_blocks:
        if b.page is not None:
            pages.append(
                {
                    "page": b.page,
                    "source_location": b.source_location,
                    "text_preview": b.text[:500],
                    "content_hash": b.content_hash,
                }
            )
    sections = []
    for b in parsed.text_blocks:
        if b.section:
            sections.append({"section": b.section, "source_location": b.source_location})
    tables = []
    for t in parsed.tables:
        tables.append(
            {
                "table_id": t.table_id,
                "name": t.name,
                "source_location": t.source_location,
                "columns": list(t.columns),
                "rows": [list(r) for r in t.rows[:100]],
            }
        )
    conf = confidence or (CONF_HIGH if text and not parsed.partial else CONF_MEDIUM)
    if parsed.warnings and "ocr" in extraction_method:
        conf = CONF_MEDIUM
    return DocumentContent(
        document_id=parsed.document_id,
        text=text,
        pages=tuple(pages),
        sections=tuple(sections),
        tables=tuple(tables),
        image_refs=(),
        metadata=dict(parsed.metadata_safe),
        extraction_method=extraction_method,
        confidence=conf if conf else CONF_LOW,
        warnings=tuple(parsed.warnings),
    )
