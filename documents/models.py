"""P14 Document / Spreadsheet models."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from memory.models import MemoryScope, utc_now
from security.encryption import (
    SENSITIVITY_INTERNAL,
    SENSITIVITY_SECRET,
    SENSITIVITY_SENSITIVE,
)

DOCUMENT_SCHEMA_VERSION = 2
DOCUMENT_POLICY_VERSION = "1.0.0"
DOCUMENT_PARSER_REGISTRY_VERSION = "1.0.0"
DOCUMENT_CHUNKER_VERSION = "1.0.0"

DOC_TXT = "txt"
DOC_MD = "md"
DOC_CSV = "csv"
DOC_XLSX = "xlsx"
DOC_XLS = "xls"
DOC_DOCX = "docx"
DOC_PDF = "pdf"
DOC_JSON = "json"
DOC_XML = "xml"
DOC_IMAGE = "image"
DOCUMENT_TYPES = (
    DOC_TXT,
    DOC_MD,
    DOC_CSV,
    DOC_XLSX,
    DOC_XLS,
    DOC_DOCX,
    DOC_PDF,
    DOC_JSON,
    DOC_XML,
    DOC_IMAGE,
)

STATUS_INGESTED = "ingested"
STATUS_PARSED = "parsed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_DELETED = "deleted"
STATUS_UNSUPPORTED = "unsupported"
DOCUMENT_STATUSES = (
    STATUS_INGESTED,
    STATUS_PARSED,
    STATUS_PARTIAL,
    STATUS_FAILED,
    STATUS_DELETED,
    STATUS_UNSUPPORTED,
)

SOURCE_USER_UPLOAD = "user_upload"
SOURCE_WORKFLOW = "workflow"
SOURCE_SYSTEM = "system"
SOURCE_OPERATOR = "operator"
SOURCE_TEST_FIXTURE = "test_fixture"
DOCUMENT_SOURCE_TYPES = (
    SOURCE_USER_UPLOAD,
    SOURCE_WORKFLOW,
    SOURCE_SYSTEM,
    SOURCE_OPERATOR,
    SOURCE_TEST_FIXTURE,
)

SENSITIVITIES = (SENSITIVITY_INTERNAL, SENSITIVITY_SENSITIVE, SENSITIVITY_SECRET)

CELL_STRING = "string"
CELL_NUMBER = "number"
CELL_BOOLEAN = "boolean"
CELL_DATE = "date"
CELL_DATETIME = "datetime"
CELL_BLANK = "blank"
CELL_ERROR = "error"
CELL_FORMULA = "formula"
CELL_TYPES = (
    CELL_STRING,
    CELL_NUMBER,
    CELL_BOOLEAN,
    CELL_DATE,
    CELL_DATETIME,
    CELL_BLANK,
    CELL_ERROR,
    CELL_FORMULA,
)

DEFAULT_MAX_FILE_BYTES = 5_000_000
DEFAULT_MAX_TEXT_BYTES = 1_000_000
DEFAULT_MAX_TABLE_CELLS = 100_000
DEFAULT_MAX_SHEETS = 50
DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_CHUNKS = 500
DEFAULT_CHUNK_MAX_CHARS = 2_000
DEFAULT_CHUNK_OVERLAP_CHARS = 100
DEFAULT_SEARCH_LIMIT = 10

_WHITESPACE_RE = re.compile(r"\s+")
_UNSAFE_NAME_RE = re.compile(r"[^\w.\- ]+", re.UNICODE)


def content_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash_text(text: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", str(text or "").strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def sanitize_filename(name: str) -> str:
    base = str(name or "unnamed").replace("\\", "/").split("/")[-1].strip()
    base = _UNSAFE_NAME_RE.sub("_", base)
    return (base or "unnamed")[:180]


def citation_ref_for(document_id: str, chunk_id: str | None = None) -> str:
    if chunk_id:
        return f"document:{document_id}#chunk:{chunk_id}"
    return f"document:{document_id}"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def _ensure_utc(stamp: datetime | None) -> datetime | None:
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class DocumentProvenance:
    source_type: str
    source_id: str
    ingested_by: str
    ingested_at: datetime
    source_hash: str = ""
    workflow_id: str | None = None
    task_id: str | None = None
    parser_version: str = ""

    def __post_init__(self):
        if self.source_type not in DOCUMENT_SOURCE_TYPES:
            raise ValueError(f"invalid_source_type:{self.source_type}")
        if not str(self.source_id or "").strip():
            raise ValueError("source_id_required")
        object.__setattr__(self, "ingested_at", _ensure_utc(self.ingested_at) or utc_now())


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    scope: MemoryScope
    filename_safe: str
    media_type: str
    document_type: str
    size_bytes: int
    content_hash: str
    source_type: str
    source_ref: str
    provenance: DocumentProvenance
    sensitivity: str
    status: str
    created_at: datetime
    updated_at: datetime
    version: int = 1
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    page_count: int | None = None
    sheet_count: int | None = None
    chunk_count: int = 0
    parser_version: str | None = None
    title: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        if self.document_type not in DOCUMENT_TYPES:
            raise ValueError(f"invalid_document_type:{self.document_type}")
        if self.status not in DOCUMENT_STATUSES:
            raise ValueError(f"invalid_document_status:{self.status}")
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(f"invalid_sensitivity:{self.sensitivity}")
        object.__setattr__(self, "filename_safe", sanitize_filename(self.filename_safe))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())
        object.__setattr__(self, "updated_at", _ensure_utc(self.updated_at) or utc_now())


@dataclass(frozen=True)
class DocumentIngestRequest:
    scope: MemoryScope
    filename: str
    content: bytes
    source_type: str
    source_id: str
    sensitivity: str = SENSITIVITY_INTERNAL
    media_type: str | None = None
    tags: tuple[str, ...] = ()
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    ingested_by: str = "document_service"
    workflow_id: str | None = None
    task_id: str | None = None
    promote_to_memory: bool = False

    def __post_init__(self):
        if self.source_type not in DOCUMENT_SOURCE_TYPES:
            raise ValueError(f"invalid_source_type:{self.source_type}")
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(f"invalid_sensitivity:{self.sensitivity}")
        if not isinstance(self.content, (bytes, bytearray)):
            raise ValueError("content_bytes_required")
        object.__setattr__(self, "content", bytes(self.content))
        object.__setattr__(self, "tags", tuple(str(t) for t in self.tags))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        object.__setattr__(self, "filename", sanitize_filename(self.filename))


@dataclass(frozen=True)
class TextBlock:
    block_id: str
    ordinal: int
    text: str
    content_hash: str
    source_location: str
    page: int | None = None
    section: str | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class TableBlock:
    table_id: str
    ordinal: int
    rows: tuple[tuple[str, ...], ...]
    columns: tuple[str, ...]
    source_location: str
    name: str | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "rows", tuple(tuple(str(c) for c in r) for r in self.rows))
        object.__setattr__(self, "columns", tuple(str(c) for c in self.columns))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class CellValue:
    row: int
    column: int
    coordinate: str
    value: str | None
    value_type: str
    formula: str | None = None
    display_value: str | None = None
    cached_value: bool = False
    potential_formula_text: bool = False
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.value_type not in CELL_TYPES:
            raise ValueError(f"invalid_cell_type:{self.value_type}")
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class CellRange:
    sheet_name: str
    start_row: int
    end_row: int
    start_column: int
    end_column: int
    a1_range: str
    cell_count: int

    def __post_init__(self):
        if self.cell_count < 0:
            raise ValueError("invalid_cell_count")
        if self.end_row < self.start_row or self.end_column < self.start_column:
            raise ValueError("invalid_range_bounds")


@dataclass(frozen=True)
class WorksheetRecord:
    sheet_name: str
    index: int
    max_row: int
    max_column: int
    visible: bool = True
    merged_ranges_count: int = 0
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class WorkbookRecord:
    document_id: str
    sheet_names: tuple[str, ...]
    sheet_count: int
    active_sheet: str | None = None
    defined_names_count: int = 0
    has_macros: bool = False
    has_external_links: bool = False
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "sheet_names", tuple(self.sheet_names))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class ParsedDocument:
    document_id: str
    text_blocks: tuple[TextBlock, ...]
    tables: tuple[TableBlock, ...]
    metadata_safe: Mapping[str, object]
    parser_id: str
    parser_version: str
    title: str | None = None
    pages: int | None = None
    sheets: tuple[WorksheetRecord, ...] = ()
    workbook: WorkbookRecord | None = None
    cells: tuple[CellValue, ...] = ()
    warnings: tuple[str, ...] = ()
    partial: bool = False

    def __post_init__(self):
        object.__setattr__(self, "text_blocks", tuple(self.text_blocks))
        object.__setattr__(self, "tables", tuple(self.tables))
        object.__setattr__(self, "sheets", tuple(self.sheets))
        object.__setattr__(self, "cells", tuple(self.cells))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class DocumentChunkRecord:
    chunk_id: str
    document_id: str
    scope: MemoryScope
    ordinal: int
    content_hash: str
    source_location: str
    content_safe: str | None = None
    encrypted_content: str | None = None
    sensitivity: str = SENSITIVITY_INTERNAL
    provenance_json: Mapping[str, object] = field(default_factory=dict)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(f"invalid_sensitivity:{self.sensitivity}")
        object.__setattr__(self, "provenance_json", _meta(self.provenance_json))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        object.__setattr__(self, "created_at", _ensure_utc(self.created_at) or utc_now())


@dataclass(frozen=True)
class DocumentSearchRequest:
    scope: MemoryScope
    query: str
    document_types: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    limit: int = DEFAULT_SEARCH_LIMIT

    def __post_init__(self):
        limit = max(1, min(int(self.limit), 20))
        object.__setattr__(self, "limit", limit)
        object.__setattr__(self, "document_types", tuple(self.document_types or ()))
        object.__setattr__(self, "tags", tuple(self.tags or ()))


@dataclass(frozen=True)
class DocumentSearchResult:
    document_id: str
    chunk_id: str
    score: float
    snippet_safe: str
    source_location: str
    provenance: Mapping[str, object]
    citation_ref: str
