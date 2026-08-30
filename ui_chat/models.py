"""UI Chat domain models — server-authoritative projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Attachment classes
ATTACH_CLASS_IMAGE = "IMAGE"
ATTACH_CLASS_DOCUMENT = "DOCUMENT"
ATTACH_CLASS_SPREADSHEET = "SPREADSHEET"
ATTACH_CLASS_TEXT = "TEXT"
ATTACH_CLASS_AUDIO = "AUDIO"
ATTACH_CLASS_UNKNOWN = "UNKNOWN"

# Attachment lifecycle
ATTACH_SELECTED = "SELECTED_LOCAL"
ATTACH_UPLOADING = "UPLOADING"
ATTACH_UPLOADED = "UPLOADED"
ATTACH_PROCESSING = "PROCESSING"
ATTACH_READY = "READY"
ATTACH_FAILED = "FAILED"
ATTACH_REMOVED = "REMOVED"

# Voice mic state (client projection hints)
VOICE_IDLE = "IDLE"
VOICE_REQUESTING = "REQUESTING_PERMISSION"
VOICE_RECORDING = "RECORDING"
VOICE_PROCESSING = "PROCESSING"
VOICE_READY = "READY"
VOICE_ERROR = "ERROR"

# Chat run states
RUN_QUEUED = "QUEUED"
RUN_RUNNING = "RUNNING"
RUN_WAITING_TOOL = "WAITING_TOOL"
RUN_WAITING_APPROVAL = "WAITING_APPROVAL"
RUN_BACKGROUND = "BACKGROUND"
RUN_SUCCEEDED = "SUCCEEDED"
RUN_FAILED = "FAILED"
RUN_CANCELLED = "CANCELLED"

# Background task states
TASK_QUEUED = "QUEUED"
TASK_RUNNING = "RUNNING"
TASK_WAITING = "WAITING"
TASK_RETRYING = "RETRYING"
TASK_SUCCEEDED = "SUCCEEDED"
TASK_FAILED = "FAILED"
TASK_CANCELLED = "CANCELLED"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_STATUS = "status"


@dataclass
class ChatConversation:
    conversation_id: str
    tenant_id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str
    status: str = "active"


@dataclass
class ChatMessage:
    message_id: str
    conversation_id: str
    tenant_id: str
    role: str
    content: str
    content_version: int
    created_at: str
    attachment_ids: tuple[str, ...] = ()
    metadata_safe: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttachmentRef:
    attachment_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None
    filename_safe: str
    attachment_class: str
    mime_type: str
    size_bytes: int
    status: str
    artifact_ref: str | None = None
    content_hash: str | None = None
    error_code: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ChatRun:
    run_id: str
    conversation_id: str
    tenant_id: str
    user_id: str
    idempotency_key: str
    status: str
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    workflow_id: str | None = None
    task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str = ""
    updated_at: str = ""
    finished_at: str | None = None


@dataclass
class VoiceTranscript:
    transcript_id: str
    tenant_id: str
    user_id: str
    text: str
    audio_attachment_id: str | None
    created_at: str


@dataclass
class VoiceAudioArtifact:
    artifact_id: str
    tenant_id: str
    message_id: str
    message_version: int
    mime_type: str
    byte_size: int
    content_hash: str
    created_at: str


@dataclass
class BackgroundTaskView:
    task_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None
    operation_label: str
    status: str
    run_id: str | None = None
    phase: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    result_artifact_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    cancel_available: bool = False
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    workflow_id: str | None = None


@dataclass
class CitationView:
    citation_id: str
    label: str
    url: str | None = None
    artifact_id: str | None = None


@dataclass
class ArtifactView:
    artifact_id: str
    label: str
    mime_type: str | None = None
    download_path: str | None = None


@dataclass
class ApprovalView:
    approval_id: str
    summary: str
    status: str
    action_label: str
    can_respond: bool = False
