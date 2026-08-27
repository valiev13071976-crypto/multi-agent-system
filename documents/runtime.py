"""DocumentRuntime composition — no hidden globals, no network/OCR at startup."""

from __future__ import annotations

import os
from pathlib import Path

from documents.access import DocumentAccessPolicy
from documents.chunker import DocumentChunker
from documents.errors import DOCUMENT_STORE_UNAVAILABLE, DocumentError
from documents.intelligence.ocr import build_ocr_provider
from documents.intelligence.raster import build_pdf_rasterizer
from documents.intelligence.service import DocumentIntelligenceService, build_document_intelligence
from documents.intelligence.large import LargeDocumentPolicy
from documents.parsers import build_default_registry
from documents.retention import DocumentRetentionPolicy
from documents.service import DocumentService
from documents.sqlite_store import SqliteDocumentStore
from documents.store import InMemoryDocumentStore
from documents.validator import DocumentValidator
from security.encryption import EncryptionService


def documents_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("DOCUMENTS_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def document_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    return {
        "enabled": documents_enabled(source),
        "backend": str(source.get("DOCUMENT_BACKEND", "memory") or "memory").strip().lower(),
        "db_path": str(source.get("DOCUMENT_DB_PATH") or "").strip() or None,
        "max_file_bytes": int(source.get("DOCUMENT_MAX_FILE_BYTES", "5000000") or 5000000),
        "max_text_bytes": int(source.get("DOCUMENT_MAX_TEXT_BYTES", "1000000") or 1000000),
        "max_table_cells": int(source.get("DOCUMENT_MAX_TABLE_CELLS", "100000") or 100000),
        "max_sheets": int(source.get("DOCUMENT_MAX_SHEETS", "50") or 50),
        "max_pages": int(source.get("DOCUMENT_MAX_PAGES", "200") or 200),
        "max_chunks": int(source.get("DOCUMENT_MAX_CHUNKS", "500") or 500),
        "chunk_max_chars": int(source.get("DOCUMENT_CHUNK_MAX_CHARS", "2000") or 2000),
        "chunk_overlap_chars": int(source.get("DOCUMENT_CHUNK_OVERLAP_CHARS", "100") or 100),
    }


def build_document_store(
    *,
    env: dict | None = None,
    shared_connection=None,
    require_durable: bool = False,
):
    cfg = document_config(env)
    backend = cfg["backend"]
    if backend in {"sqlite", "durable"} or cfg["db_path"]:
        path = cfg["db_path"]
        if shared_connection is not None and not path:
            return SqliteDocumentStore(shared_connection=shared_connection, owns_connection=False)
        if path:
            return SqliteDocumentStore(db_path=path, owns_connection=True)
        if require_durable:
            raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
        if shared_connection is not None:
            return SqliteDocumentStore(shared_connection=shared_connection, owns_connection=False)
        raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
    if require_durable:
        raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
    return InMemoryDocumentStore()


class DocumentRuntime:
    def __init__(
        self,
        *,
        service: DocumentService,
        store,
        registry,
        chunker: DocumentChunker,
        access: DocumentAccessPolicy,
        validator: DocumentValidator,
        retention: DocumentRetentionPolicy | None = None,
        enabled: bool = True,
        ocr_provider=None,
        rasterizer=None,
        intelligence: DocumentIntelligenceService | None = None,
    ):
        self.service = service
        self.store = store
        self.registry = registry
        self.chunker = chunker
        self.access = access
        self.validator = validator
        self.retention = retention or DocumentRetentionPolicy()
        self.enabled = bool(enabled)
        self.ocr_provider = ocr_provider
        self.rasterizer = rasterizer
        self.intelligence = intelligence

    def health(self) -> dict:
        ready = bool(getattr(self.store, "available", True))
        status = "healthy"
        supported = set(self.registry.list_supported_types())
        optional_missing = {"docx", "pdf", "xlsx"} - supported
        if optional_missing:
            status = "degraded"
        if self.enabled and (not ready or self.service.blocked_reason):
            status = "blocked"
        ocr = self.ocr_provider
        rast = self.rasterizer
        return {
            "document_status": status,
            "persistence_backend": getattr(self.store, "persistence_backend", "memory"),
            "persistence_ready": ready,
            "connection_mode": getattr(self.store, "connection_mode", "memory"),
            "supported_types": sorted(supported),
            "enabled": self.enabled,
            "ocr_provider": getattr(ocr, "provider_id", "null"),
            "ocr_available": bool(getattr(ocr, "available", False)),
            "rasterizer": getattr(rast, "provider_id", "null"),
            "rasterizer_available": bool(getattr(rast, "available", False)),
            "intelligence_ready": self.intelligence is not None,
        }

    def close(self) -> None:
        if hasattr(self.store, "close"):
            try:
                self.store.close()
            except Exception:
                pass


def build_document_runtime(
    *,
    env: dict | None = None,
    encryption: EncryptionService | None = None,
    observability=None,
    memory_service=None,
    shared_connection=None,
    allowed_roots: tuple[str, ...] = (),
) -> DocumentRuntime | None:
    cfg = document_config(env)
    if not cfg["enabled"]:
        return None
    require_durable = cfg["backend"] in {"sqlite", "durable"}
    try:
        store = build_document_store(
            env=env,
            shared_connection=shared_connection,
            require_durable=require_durable,
        )
    except DocumentError:
        if require_durable:
            store = InMemoryDocumentStore()
            store.available = False
            ocr = build_ocr_provider(env)
            rast = build_pdf_rasterizer(env)
            registry = build_default_registry(
                max_file_bytes=cfg["max_file_bytes"], ocr_provider=ocr
            )
            intelligence = build_document_intelligence(
                env=env, ocr_provider=ocr, rasterizer=rast
            )
            service = DocumentService(
                store,
                registry=registry,
                encryption=encryption,
                observability=observability,
                memory_service=memory_service,
                limits=cfg,
                allowed_roots=allowed_roots,
                enabled=True,
                intelligence=intelligence,
            )
            service.blocked_reason = DOCUMENT_STORE_UNAVAILABLE
            intelligence.documents = service
            return DocumentRuntime(
                service=service,
                store=store,
                registry=registry,
                chunker=DocumentChunker(
                    max_chars=cfg["chunk_max_chars"],
                    overlap_chars=cfg["chunk_overlap_chars"],
                    max_chunks=cfg["max_chunks"],
                ),
                access=DocumentAccessPolicy(),
                validator=DocumentValidator(),
                enabled=True,
                ocr_provider=ocr,
                rasterizer=rast,
                intelligence=intelligence,
            )
        raise

    ocr = build_ocr_provider(env)
    rast = build_pdf_rasterizer(env)
    registry = build_default_registry(max_file_bytes=cfg["max_file_bytes"], ocr_provider=ocr)
    access = DocumentAccessPolicy()
    chunker = DocumentChunker(
        max_chars=cfg["chunk_max_chars"],
        overlap_chars=cfg["chunk_overlap_chars"],
        max_chunks=cfg["max_chunks"],
    )
    validator = DocumentValidator()
    intelligence = build_document_intelligence(
        env=env, ocr_provider=ocr, rasterizer=rast
    )
    service = DocumentService(
        store,
        registry=registry,
        access=access,
        chunker=chunker,
        validator=validator,
        encryption=encryption,
        memory_service=memory_service,
        observability=observability,
        limits=cfg,
        allowed_roots=allowed_roots,
        enabled=True,
        intelligence=intelligence,
    )
    intelligence.documents = service
    return DocumentRuntime(
        service=service,
        store=store,
        registry=registry,
        chunker=chunker,
        access=access,
        validator=validator,
        retention=DocumentRetentionPolicy(),
        enabled=True,
        ocr_provider=ocr,
        rasterizer=rast,
        intelligence=intelligence,
    )


def attach_workflow_runtime(document_runtime: DocumentRuntime | None, workflow_runtime) -> None:
    """Wire WorkflowRuntimeBundle into document intelligence after composition."""
    if document_runtime is None or workflow_runtime is None:
        return
    document_runtime.service.workflow_runtime = workflow_runtime
    if document_runtime.intelligence is not None:
        document_runtime.intelligence.workflow_runtime = workflow_runtime

