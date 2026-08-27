"""Legacy XLS parser — soft dependency on xlrd."""

from __future__ import annotations

import uuid

from documents.errors import DOCUMENT_PARSE_FAILED, DOCUMENT_TOO_LARGE, DocumentError
from documents.models import (
    DOC_XLS,
    CELL_NUMBER,
    CELL_STRING,
    CellValue,
    ParsedDocument,
    TableBlock,
    TextBlock,
    WorksheetRecord,
    WorkbookRecord,
    content_hash_text,
)


class XlsDocumentParser:
    parser_id = "xls_v1"
    version = "1.0.0"
    supported_types = (DOC_XLS,)

    def parse(self, *, document_id: str, data: bytes, filename: str, limits: dict) -> ParsedDocument:
        try:
            import xlrd  # type: ignore
        except ImportError as exc:
            raise DocumentError(DOCUMENT_PARSE_FAILED) from exc

        max_sheets = int(limits.get("max_sheets", 50))
        max_cells = int(limits.get("max_table_cells", 100_000))
        try:
            book = xlrd.open_workbook(file_contents=data, formatting_info=False)
        except Exception as exc:
            raise DocumentError(DOCUMENT_PARSE_FAILED) from exc

        if book.nsheets > max_sheets:
            raise DocumentError(DOCUMENT_TOO_LARGE)

        sheets = []
        tables = []
        cells = []
        text_parts = []
        cell_count = 0
        for idx in range(book.nsheets):
            sheet = book.sheet_by_index(idx)
            sheets.append(
                WorksheetRecord(
                    sheet_name=sheet.name,
                    index=idx,
                    max_row=sheet.nrows,
                    max_column=sheet.ncols,
                )
            )
            rows = []
            for r in range(min(sheet.nrows, 200)):
                row_vals = []
                for c in range(min(sheet.ncols, 50)):
                    cell_count += 1
                    if cell_count > max_cells:
                        raise DocumentError(DOCUMENT_TOO_LARGE)
                    val = sheet.cell_value(r, c)
                    row_vals.append(str(val) if val != "" else "")
                    if val != "" and len(cells) < 5_000:
                        cells.append(
                            CellValue(
                                row=r + 1,
                                column=c + 1,
                                coordinate=f"R{r + 1}C{c + 1}",
                                value=str(val),
                                value_type=CELL_NUMBER if isinstance(val, (int, float)) else CELL_STRING,
                            )
                        )
                rows.append(tuple(row_vals))
            if rows:
                tables.append(
                    TableBlock(
                        table_id=str(uuid.uuid4()),
                        ordinal=len(tables),
                        rows=tuple(rows[:50]),
                        columns=tuple(f"c{i}" for i in range(len(rows[0]))),
                        source_location=f"xls:sheet:{sheet.name}",
                        name=sheet.name,
                    )
                )
                text_parts.append(sheet.name + "\n" + "\n".join("\t".join(r) for r in rows[:20]))

        body = "\n\n".join(text_parts)[: int(limits.get("max_text_bytes", 1_000_000))]
        blocks = ()
        if body:
            blocks = (
                TextBlock(
                    block_id=str(uuid.uuid4()),
                    ordinal=0,
                    text=body,
                    content_hash=content_hash_text(body),
                    source_location="xls:workbook",
                ),
            )
        return ParsedDocument(
            document_id=document_id,
            text_blocks=blocks,
            tables=tuple(tables),
            metadata_safe={"filename": filename, "sheet_count": book.nsheets},
            parser_id=self.parser_id,
            parser_version=self.version,
            title=filename,
            sheets=tuple(sheets),
            workbook=WorkbookRecord(
                document_id=document_id,
                sheet_names=tuple(s.sheet_name for s in sheets),
                sheet_count=len(sheets),
            ),
            cells=tuple(cells),
            warnings=("xls_legacy_parser",),
        )
