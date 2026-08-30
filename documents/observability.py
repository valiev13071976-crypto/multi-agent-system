"""Document observability — lifecycle events, no full text / OCR / tables / secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

_REDACT_KEYS = frozenset(
    {
        "secret",
        "password",
        "token",
        "api_key",
        "authorization",
        "credential",
        "body",
        "body_text",
        "content",
        "content_text",
        "text",
        "native_text",
        "ocr_text",
        "raw",
        "raw_ocr",
        "table",
        "tables",
        "rows",
        "cells",
        "html",
        "filename",
        "path",
        "query",
    }
)


def sanitize_event_payload(payload: dict | None) -> dict:
    out = {}
    for key, value in dict(payload or {}).items():
        lk = str(key).lower()
        if any(s in lk for s in _REDACT_KEYS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > 256:
                out[key] = value[:256]
            else:
                out[key] = value
        elif isinstance(value, (list, tuple)):
            # Bound cardinality; drop nested content-like structures
            safe_list = []
            for item in list(value)[:32]:
                if isinstance(item, dict):
                    safe_list.append(sanitize_event_payload(item))
                elif isinstance(item, (str, int, float, bool)) or item is None:
                    safe_list.append(item[:128] if isinstance(item, str) and len(item) > 128 else item)
            out[key] = safe_list
        elif isinstance(value, dict):
            out[key] = sanitize_event_payload(value)
    return out


@dataclass
class DocumentMetrics:
    ingested: int = 0
    processing_started: int = 0
    native_extracted: int = 0
    ocr_required: int = 0
    ocr_performed: int = 0
    ocr_failed: int = 0
    classified: int = 0
    extracted: int = 0
    validated: int = 0
    compared: int = 0
    reconciled: int = 0
    generated: int = 0
    failed: int = 0
    cancelled: int = 0

    def as_dict(self) -> dict:
        return {
            "ingested": self.ingested,
            "processing_started": self.processing_started,
            "native_extracted": self.native_extracted,
            "ocr_required": self.ocr_required,
            "ocr_performed": self.ocr_performed,
            "ocr_failed": self.ocr_failed,
            "classified": self.classified,
            "extracted": self.extracted,
            "validated": self.validated,
            "compared": self.compared,
            "reconciled": self.reconciled,
            "generated": self.generated,
            "failed": self.failed,
            "cancelled": self.cancelled,
        }


@dataclass
class DocumentObserver:
    metrics: DocumentMetrics = field(default_factory=DocumentMetrics)
    _sinks: list[Callable[[str, dict], None]] = field(default_factory=list)

    def add_sink(self, sink: Callable[[str, dict], None]) -> None:
        self._sinks.append(sink)

    def emit(self, event: str, payload: dict | None = None) -> None:
        safe = sanitize_event_payload(payload)
        safe.setdefault("event", event)
        for sink in self._sinks:
            try:
                sink(event, safe)
            except Exception:
                pass
        try:
            from observability.events import emit_event

            emit_event(event, safe)
        except Exception:
            pass

    def on_ingested(self, *, document_id: str, tenant_id: str, media_type: str = "") -> None:
        self.metrics.ingested += 1
        self.emit(
            "document.ingested",
            {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "media_type": media_type,
            },
        )

    def on_processing_started(self, *, job_id: str, tenant_id: str, stage: str = "") -> None:
        self.metrics.processing_started += 1
        self.emit(
            "document.processing.started",
            {"job_id": job_id, "tenant_id": tenant_id, "stage": stage},
        )

    def on_native_extracted(self, *, document_id: str, tenant_id: str, char_count: int = 0) -> None:
        self.metrics.native_extracted += 1
        self.emit(
            "document.native_extracted",
            {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "char_count": int(char_count),
            },
        )

    def on_ocr(self, *, status: str, document_id: str = "", tenant_id: str = "", page_count: int = 0) -> None:
        if status in {"required", "not_required"}:
            if status == "required":
                self.metrics.ocr_required += 1
            self.emit(
                f"document.ocr.{status}",
                {
                    "document_id": document_id,
                    "tenant_id": tenant_id,
                    "page_count": int(page_count),
                },
            )
        elif status in {"performed", "partial"}:
            self.metrics.ocr_performed += 1
            self.emit(
                f"document.ocr.{status}",
                {
                    "document_id": document_id,
                    "tenant_id": tenant_id,
                    "page_count": int(page_count),
                },
            )
        elif status in {"failed", "unavailable"}:
            self.metrics.ocr_failed += 1
            self.emit(
                f"document.ocr.{status}",
                {
                    "document_id": document_id,
                    "tenant_id": tenant_id,
                    "page_count": int(page_count),
                },
            )
        else:
            self.emit(
                f"document.ocr.{status}",
                {
                    "document_id": document_id,
                    "tenant_id": tenant_id,
                    "page_count": int(page_count),
                },
            )

    def on_classified(self, *, document_id: str, tenant_id: str, doc_class: str = "") -> None:
        self.metrics.classified += 1
        self.emit(
            "document.classified",
            {"document_id": document_id, "tenant_id": tenant_id, "doc_class": doc_class},
        )

    def on_extracted(self, *, document_id: str, tenant_id: str, field_count: int = 0) -> None:
        self.metrics.extracted += 1
        self.emit(
            "document.extracted",
            {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "field_count": int(field_count),
            },
        )

    def on_validated(self, *, document_id: str, tenant_id: str, ok: bool = True) -> None:
        self.metrics.validated += 1
        self.emit(
            "document.validated",
            {"document_id": document_id, "tenant_id": tenant_id, "ok": bool(ok)},
        )

    def on_compared(self, *, tenant_id: str, unchanged: bool = False) -> None:
        self.metrics.compared += 1
        self.emit("document.compared", {"tenant_id": tenant_id, "unchanged": bool(unchanged)})

    def on_reconciled(self, *, tenant_id: str, status: str = "") -> None:
        self.metrics.reconciled += 1
        self.emit("document.reconciled", {"tenant_id": tenant_id, "status": status})

    def on_generated(self, *, tenant_id: str, format: str = "") -> None:
        self.metrics.generated += 1
        self.emit("document.generated", {"tenant_id": tenant_id, "format": format})

    def on_failed(self, *, tenant_id: str, reason: str = "", job_id: str = "") -> None:
        self.metrics.failed += 1
        self.emit(
            "document.failed",
            {"tenant_id": tenant_id, "reason": reason, "job_id": job_id},
        )

    def on_cancelled(self, *, tenant_id: str, job_id: str = "") -> None:
        self.metrics.cancelled += 1
        self.emit("document.cancelled", {"tenant_id": tenant_id, "job_id": job_id})


_DEFAULT_OBSERVER: DocumentObserver | None = None


def get_observer() -> DocumentObserver:
    global _DEFAULT_OBSERVER
    if _DEFAULT_OBSERVER is None:
        _DEFAULT_OBSERVER = DocumentObserver()
    return _DEFAULT_OBSERVER


def set_observer(observer: DocumentObserver | None) -> None:
    global _DEFAULT_OBSERVER
    _DEFAULT_OBSERVER = observer
