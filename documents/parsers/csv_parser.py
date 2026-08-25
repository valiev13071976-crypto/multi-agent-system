"""Safe bounded CSV parser — no spreadsheet execution."""

from __future__ import annotations

import csv
import io
import uuid

from documents.errors import DOCUMENT_PARSE_FAILED, DOCUMENT_TOO_LARGE, DOCUMENT_TOO_MANY_CELLS, DocumentError
from documents.models import DOC_CSV, ParsedDocument, TableBlock, TextBlock, content_hash_text


_FORMULA_PREFIXES = ("=", "+", "-", "@")


class CsvDocumentParser:
    parser_id = "csv_v1"
    version = "1.0.0"
    supported_types = (DOC_CSV,)

    def parse(self, *, document_id: str, data: bytes, filename: str, limits: dict) -> ParsedDocument:
        max_text = int(limits.get("max_text_bytes", 1_000_000))
        max_cells = int(limits.get("max_table_cells", 100_000))
        if len(data) > max_text:
            raise DocumentError(DOCUMENT_TOO_LARGE)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("latin-1")
            except Exception as exc:
                raise DocumentError(DOCUMENT_PARSE_FAILED) from exc
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        rows = []
        cell_count = 0
        formula_like = 0
        for row in reader:
            normalized = []
            for cell in row:
                value = str(cell)
                if value[:1] in _FORMULA_PREFIXES:
                    formula_like += 1
                normalized.append(value)
                cell_count += 1
                if cell_count > max_cells:
                    raise DocumentError(DOCUMENT_TOO_MANY_CELLS)
            rows.append(tuple(normalized))
        if not rows:
            return ParsedDocument(
                document_id=document_id,
                text_blocks=(),
                tables=(),
                metadata_safe={"filename": filename, "empty": True},
                parser_id=self.parser_id,
                parser_version=self.version,
                title=filename,
            )
        headers = rows[0]
        body = rows[1:] if len(rows) > 1 else ()
        # Cap stored rows for bounded representation
        max_store_rows = min(len(body), 500)
        stored = body[:max_store_rows]
        table = TableBlock(
            table_id=str(uuid.uuid4()),
            ordinal=0,
            name="csv",
            rows=stored,
            columns=headers,
            source_location="csv:table:0",
            metadata_safe={
                "row_count": len(body),
                "stored_rows": len(stored),
                "formula_like_cells": formula_like,
            },
        )
        preview = "\n".join(",".join(r) for r in rows[:20])
        text_block = TextBlock(
            block_id=str(uuid.uuid4()),
            ordinal=0,
            text=preview[: max(1, max_text)],
            content_hash=content_hash_text(preview),
            source_location="csv:preview",
        )
        return ParsedDocument(
            document_id=document_id,
            text_blocks=(text_block,),
            tables=(table,),
            metadata_safe={
                "filename": filename,
                "delimiter": getattr(dialect, "delimiter", ","),
                "formula_like_cells": formula_like,
            },
            parser_id=self.parser_id,
            parser_version=self.version,
            title=filename,
        )
