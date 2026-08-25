"""Document / spreadsheet output validation."""

from __future__ import annotations

from documents.errors import DOCUMENT_PARSE_FAILED, DocumentError
from documents.models import ParsedDocument


class DocumentValidator:
    def validate_parsed(self, parsed: ParsedDocument, *, limits: dict) -> None:
        if not parsed.parser_id or not parsed.parser_version:
            raise DocumentError(DOCUMENT_PARSE_FAILED)
        max_cells = int(limits.get("max_table_cells", 100_000))
        cell_total = 0
        for table in parsed.tables:
            cell_total += len(table.columns) + sum(len(r) for r in table.rows)
            if cell_total > max_cells:
                raise DocumentError("document_too_many_cells")
            if not table.source_location:
                raise DocumentError(DOCUMENT_PARSE_FAILED)
        for block in parsed.text_blocks:
            if not block.content_hash or not block.source_location:
                raise DocumentError(DOCUMENT_PARSE_FAILED)
        if parsed.workbook is not None:
            if parsed.workbook.sheet_count != len(parsed.workbook.sheet_names):
                raise DocumentError(DOCUMENT_PARSE_FAILED)
            if parsed.workbook.has_macros:
                raise DocumentError("document_macros_not_allowed")
        # Formulas must never appear as executed results without flag
        for cell in parsed.cells:
            if cell.value_type == "formula" and cell.formula is None:
                raise DocumentError(DOCUMENT_PARSE_FAILED)
