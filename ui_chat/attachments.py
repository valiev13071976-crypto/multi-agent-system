"""Attachment ingestion — routes to Blocks 6/7/10."""

from __future__ import annotations

import os
from typing import Any

from ui_chat.errors import ATTACHMENT_UNSUPPORTED, ATTACHMENT_TOO_LARGE, UIChatError
from ui_chat.models import (
    ATTACH_CLASS_DOCUMENT,
    ATTACH_CLASS_IMAGE,
    ATTACH_CLASS_SPREADSHEET,
    ATTACH_CLASS_TEXT,
    ATTACH_CLASS_UNKNOWN,
    ATTACH_PROCESSING,
    ATTACH_READY,
    ATTACH_FAILED,
    AttachmentRef,
)

_SPREADSHEET_EXT = frozenset({".xlsx", ".xls", ".csv"})
_DOCUMENT_EXT = frozenset({".pdf", ".doc", ".docx", ".txt", ".md", ".rtf"})
_IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_TEXT_EXT = frozenset({".txt", ".md", ".json", ".xml", ".yaml", ".yml"})


def classify_attachment(filename: str, mime_type: str) -> str:
    lower = (filename or "").lower()
    for ext in _IMAGE_EXT:
        if lower.endswith(ext):
            return ATTACH_CLASS_IMAGE
    for ext in _SPREADSHEET_EXT:
        if lower.endswith(ext):
            return ATTACH_CLASS_SPREADSHEET
    for ext in _DOCUMENT_EXT:
        if lower.endswith(ext):
            return ATTACH_CLASS_DOCUMENT
    for ext in _TEXT_EXT:
        if lower.endswith(ext):
            return ATTACH_CLASS_TEXT
    mime = (mime_type or "").lower()
    if mime.startswith("image/"):
        return ATTACH_CLASS_IMAGE
    if mime in {"application/pdf"}:
        return ATTACH_CLASS_DOCUMENT
    if mime in {
        "text/csv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return ATTACH_CLASS_SPREADSHEET
    return ATTACH_CLASS_UNKNOWN


class AttachmentLimits:
    def __init__(
        self,
        *,
        max_file_bytes: int = 10 * 1024 * 1024,
        max_attachments_per_turn: int = 8,
        max_aggregate_bytes: int = 25 * 1024 * 1024,
    ):
        self.max_file_bytes = max_file_bytes
        self.max_attachments_per_turn = max_attachments_per_turn
        self.max_aggregate_bytes = max_aggregate_bytes

    @classmethod
    def from_env(cls, env: dict | None = None) -> "AttachmentLimits":
        source = env if env is not None else os.environ
        return cls(
            max_file_bytes=int(source.get("UI_CHAT_MAX_UPLOAD_BYTES") or str(10 * 1024 * 1024)),
            max_attachments_per_turn=int(source.get("UI_CHAT_MAX_ATTACHMENTS") or "8"),
            max_aggregate_bytes=int(source.get("UI_CHAT_MAX_AGGREGATE_BYTES") or str(25 * 1024 * 1024)),
        )


class AttachmentRouter:
    """Governed upload routing to existing platform services."""

    def __init__(
        self,
        *,
        limits: AttachmentLimits | None = None,
        document_service=None,
        data_intel_service=None,
        product_media_service=None,
    ):
        self.limits = limits or AttachmentLimits.from_env()
        self.document_service = document_service
        self.data_intel_service = data_intel_service
        self.product_media_service = product_media_service

    def validate_upload(self, *, data: bytes, filename: str, mime_type: str) -> str:
        if len(data) > self.limits.max_file_bytes:
            raise UIChatError(ATTACHMENT_TOO_LARGE, message="File exceeds upload limit.")
        cls = classify_attachment(filename, mime_type)
        if cls == ATTACH_CLASS_UNKNOWN:
            raise UIChatError(ATTACHMENT_UNSUPPORTED, message="Unsupported attachment type.")
        return cls

    def ingest(
        self,
        ref: AttachmentRef,
        *,
        data: bytes,
        tenant_id: str,
        memory_scope=None,
    ) -> tuple[AttachmentRef, dict[str, Any] | None]:
        cls = self.validate_upload(data=data, filename=ref.filename_safe, mime_type=ref.mime_type)
        ref.attachment_class = cls
        background: dict[str, Any] | None = None

        if cls == ATTACH_CLASS_IMAGE:
            if self.product_media_service is None:
                raise UIChatError(ATTACHMENT_UNSUPPORTED, message="Image processing unavailable.")
            version = self.product_media_service.ingest(
                tenant_id=tenant_id,
                data=data,
                filename=ref.filename_safe,
                declared_mime=ref.mime_type,
            )
            ref.artifact_ref = version.version_id
            ref.status = ATTACH_READY
            return ref, background

        if cls == ATTACH_CLASS_SPREADSHEET:
            if self.data_intel_service is None:
                raise UIChatError(ATTACHMENT_UNSUPPORTED, message="Spreadsheet processing unavailable.")
            result = self.data_intel_service.ingest(
                data,
                filename=ref.filename_safe,
                tenant_id=tenant_id,
            )
            ref.artifact_ref = result.get("dataset_id") if isinstance(result, dict) else getattr(result, "dataset_id", None) or str(result)
            batch = bool(
                (isinstance(result, dict) and (result.get("async") or result.get("batch_required")))
                or getattr(result, "batch_required", False)
            )
            if isinstance(result, dict) and result.get("workflow_id"):
                bg_workflow = result.get("workflow_id")
            else:
                bg_workflow = getattr(result, "workflow_id", None)
            if batch:
                ref.status = ATTACH_PROCESSING
                background = {
                    "operation": "spreadsheet_batch",
                    "workflow_id": bg_workflow if batch else getattr(result, "workflow_id", None) if not isinstance(result, dict) else result.get("workflow_id"),
                    "task_id": getattr(result, "task_id", None) if not isinstance(result, dict) else result.get("task_id"),
                }
            else:
                ref.status = ATTACH_READY
            return ref, background

        if cls in {ATTACH_CLASS_DOCUMENT, ATTACH_CLASS_TEXT}:
            if self.document_service is None:
                raise UIChatError(ATTACHMENT_UNSUPPORTED, message="Document processing unavailable.")
            from documents.models import DocumentIngestRequest, SOURCE_UPLOAD

            req = DocumentIngestRequest(
                content=data,
                filename=ref.filename_safe,
                media_type=ref.mime_type,
                scope=memory_scope,
                source_type=SOURCE_UPLOAD,
                source_id=ref.attachment_id,
                ingested_by=ref.user_id,
            )
            record = self.document_service.ingest(req, requesting_scope=memory_scope)
            ref.artifact_ref = record.document_id
            if record.status in {"processing", "queued", "extracting"}:
                ref.status = ATTACH_PROCESSING
                background = {
                    "operation": "document_extract",
                    "workflow_id": getattr(record, "workflow_id", None),
                    "document_id": record.document_id,
                }
            elif record.status in {"failed", "deleted"}:
                ref.status = ATTACH_FAILED
                ref.error_code = "document_failed"
            else:
                ref.status = ATTACH_READY
            return ref, background

        ref.status = ATTACH_FAILED
        ref.error_code = ATTACHMENT_UNSUPPORTED
        return ref, background
