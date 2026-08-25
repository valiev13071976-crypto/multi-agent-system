"""Parser interface and registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from documents.errors import UNSUPPORTED_DOCUMENT_TYPE, DocumentError
from documents.models import DOCUMENT_PARSER_REGISTRY_VERSION, ParsedDocument


@runtime_checkable
class DocumentParser(Protocol):
    parser_id: str
    version: str
    supported_types: tuple[str, ...]

    def parse(self, *, document_id: str, data: bytes, filename: str, limits: dict) -> ParsedDocument:
        ...


@dataclass(frozen=True)
class ParserDescriptor:
    parser_id: str
    version: str
    supported_types: tuple[str, ...]
    max_size: int
    capabilities: tuple[str, ...]
    enabled: bool = True


class DocumentParserRegistry:
    registry_version = DOCUMENT_PARSER_REGISTRY_VERSION

    def __init__(self):
        self._parsers: dict[str, DocumentParser] = {}
        self._descriptors: dict[str, ParserDescriptor] = {}
        self._frozen = False

    def register(self, parser: DocumentParser, *, max_size: int, capabilities: tuple[str, ...] = ()) -> None:
        if self._frozen:
            raise RuntimeError("parser_registry_frozen")
        self._parsers[parser.parser_id] = parser
        self._descriptors[parser.parser_id] = ParserDescriptor(
            parser_id=parser.parser_id,
            version=parser.version,
            supported_types=tuple(parser.supported_types),
            max_size=int(max_size),
            capabilities=tuple(capabilities),
            enabled=True,
        )

    def freeze(self) -> None:
        self._frozen = True

    def get_parser(self, document_type: str) -> DocumentParser:
        for parser in self._parsers.values():
            desc = self._descriptors[parser.parser_id]
            if desc.enabled and document_type in desc.supported_types:
                return parser
        raise DocumentError(UNSUPPORTED_DOCUMENT_TYPE)

    def list_supported_types(self) -> tuple[str, ...]:
        types = set()
        for desc in self._descriptors.values():
            if desc.enabled:
                types.update(desc.supported_types)
        return tuple(sorted(types))

    def descriptors(self) -> tuple[ParserDescriptor, ...]:
        return tuple(self._descriptors.values())


def build_default_registry(*, max_file_bytes: int) -> DocumentParserRegistry:
    from documents.parsers.csv_parser import CsvDocumentParser
    from documents.parsers.docx_parser import DocxDocumentParser
    from documents.parsers.md import MarkdownDocumentParser
    from documents.parsers.pdf import PdfDocumentParser
    from documents.parsers.txt import TxtDocumentParser
    from documents.parsers.xlsx import XlsxDocumentParser

    reg = DocumentParserRegistry()
    for parser, caps in (
        (TxtDocumentParser(), ("text",)),
        (MarkdownDocumentParser(), ("text", "headings")),
        (CsvDocumentParser(), ("table", "csv")),
        (XlsxDocumentParser(), ("workbook", "sheets", "cells", "formulas_as_data")),
        (DocxDocumentParser(), ("paragraphs", "tables")),
        (PdfDocumentParser(), ("text_pages", "no_ocr")),
    ):
        reg.register(parser, max_size=max_file_bytes, capabilities=caps)
    reg.freeze()
    return reg


def parser_registry_snapshot(registry: DocumentParserRegistry | None = None) -> dict:
    reg = registry or build_default_registry(max_file_bytes=5_000_000)
    return {
        "document_parser_registry_version": DOCUMENT_PARSER_REGISTRY_VERSION,
        "supported_types": list(reg.list_supported_types()),
        "parsers": [
            {
                "parser_id": d.parser_id,
                "version": d.version,
                "supported_types": list(d.supported_types),
                "capabilities": list(d.capabilities),
            }
            for d in sorted(reg.descriptors(), key=lambda x: x.parser_id)
        ],
    }
