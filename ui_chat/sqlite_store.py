"""SQLite-backed UI Chat store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ui_chat.models import (
    AttachmentRef,
    BackgroundTaskView,
    ChatConversation,
    ChatMessage,
    ChatRun,
    VoiceAudioArtifact,
    VoiceTranscript,
)


class SqliteUIChatStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    @property
    def available(self) -> bool:
        return True

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_conversations (
                conversation_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_conv_tenant_user
                ON chat_conversations(tenant_id, user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS chat_messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                content_version INTEGER NOT NULL DEFAULT 1,
                attachment_ids TEXT NOT NULL DEFAULT '[]',
                metadata_safe TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_msg_conv
                ON chat_messages(conversation_id, created_at);

            CREATE TABLE IF NOT EXISTS chat_attachments (
                attachment_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                conversation_id TEXT,
                filename_safe TEXT NOT NULL,
                attachment_class TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                artifact_ref TEXT,
                content_hash TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_runs (
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL,
                user_message_id TEXT,
                assistant_message_id TEXT,
                workflow_id TEXT,
                task_id TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE(tenant_id, conversation_id, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS chat_transcripts (
                transcript_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                audio_attachment_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_voice_artifacts (
                artifact_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                message_version INTEGER NOT NULL,
                mime_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                blob BLOB NOT NULL,
                UNIQUE(tenant_id, message_id, message_version)
            );

            CREATE TABLE IF NOT EXISTS chat_background_tasks (
                task_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                conversation_id TEXT,
                run_id TEXT,
                operation_label TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT,
                progress_current INTEGER,
                progress_total INTEGER,
                result_artifact_ids TEXT NOT NULL DEFAULT '[]',
                error_code TEXT,
                error_message TEXT,
                cancel_available INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                workflow_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chat_tasks_tenant_user
                ON chat_background_tasks(tenant_id, user_id, created_at DESC);
            """
        )
        self._conn.commit()

    def create_conversation(self, conv: ChatConversation) -> ChatConversation:
        self._conn.execute(
            """
            INSERT INTO chat_conversations
            (conversation_id, tenant_id, user_id, title, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conv.conversation_id,
                conv.tenant_id,
                conv.user_id,
                conv.title,
                conv.status,
                conv.created_at,
                conv.updated_at,
            ),
        )
        self._conn.commit()
        return conv

    def update_conversation(self, conv: ChatConversation) -> ChatConversation:
        self._conn.execute(
            """
            UPDATE chat_conversations
            SET title = ?, status = ?, updated_at = ?
            WHERE conversation_id = ? AND tenant_id = ?
            """,
            (conv.title, conv.status, conv.updated_at, conv.conversation_id, conv.tenant_id),
        )
        self._conn.commit()
        return conv

    def list_conversations(self, *, tenant_id: str, user_id: str, limit: int = 50) -> list[ChatConversation]:
        rows = self._conn.execute(
            """
            SELECT * FROM chat_conversations
            WHERE tenant_id = ? AND user_id = ?
            ORDER BY updated_at DESC LIMIT ?
            """,
            (tenant_id, user_id, limit),
        ).fetchall()
        return [self._conv_row(r) for r in rows]

    def get_conversation(self, conversation_id: str, *, tenant_id: str) -> ChatConversation | None:
        row = self._conn.execute(
            "SELECT * FROM chat_conversations WHERE conversation_id = ? AND tenant_id = ?",
            (conversation_id, tenant_id),
        ).fetchone()
        return self._conv_row(row) if row else None

    def add_message(self, msg: ChatMessage) -> ChatMessage:
        self._conn.execute(
            """
            INSERT INTO chat_messages
            (message_id, conversation_id, tenant_id, role, content, content_version,
             attachment_ids, metadata_safe, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.message_id,
                msg.conversation_id,
                msg.tenant_id,
                msg.role,
                msg.content,
                msg.content_version,
                json.dumps(list(msg.attachment_ids)),
                json.dumps(msg.metadata_safe),
                msg.created_at,
            ),
        )
        self._conn.commit()
        return msg

    def list_messages(self, conversation_id: str, *, tenant_id: str, limit: int = 200) -> list[ChatMessage]:
        rows = self._conn.execute(
            """
            SELECT * FROM chat_messages
            WHERE conversation_id = ? AND tenant_id = ?
            ORDER BY created_at ASC LIMIT ?
            """,
            (conversation_id, tenant_id, limit),
        ).fetchall()
        return [self._msg_row(r) for r in rows]

    def get_message(self, message_id: str, *, tenant_id: str) -> ChatMessage | None:
        row = self._conn.execute(
            "SELECT * FROM chat_messages WHERE message_id = ? AND tenant_id = ?",
            (message_id, tenant_id),
        ).fetchone()
        return self._msg_row(row) if row else None

    def save_attachment(self, ref: AttachmentRef) -> AttachmentRef:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO chat_attachments
            (attachment_id, tenant_id, user_id, conversation_id, filename_safe,
             attachment_class, mime_type, size_bytes, status, artifact_ref,
             content_hash, error_code, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref.attachment_id,
                ref.tenant_id,
                ref.user_id,
                ref.conversation_id,
                ref.filename_safe,
                ref.attachment_class,
                ref.mime_type,
                ref.size_bytes,
                ref.status,
                ref.artifact_ref,
                ref.content_hash,
                ref.error_code,
                ref.created_at,
                ref.updated_at,
            ),
        )
        self._conn.commit()
        return ref

    def get_attachment(self, attachment_id: str, *, tenant_id: str) -> AttachmentRef | None:
        row = self._conn.execute(
            "SELECT * FROM chat_attachments WHERE attachment_id = ? AND tenant_id = ?",
            (attachment_id, tenant_id),
        ).fetchone()
        return self._attach_row(row) if row else None

    def update_attachment(self, ref: AttachmentRef) -> AttachmentRef:
        return self.save_attachment(ref)

    def save_run(self, run: ChatRun) -> ChatRun:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO chat_runs
            (run_id, conversation_id, tenant_id, user_id, idempotency_key, status,
             user_message_id, assistant_message_id, workflow_id, task_id,
             error_code, error_message, created_at, updated_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.conversation_id,
                run.tenant_id,
                run.user_id,
                run.idempotency_key,
                run.status,
                run.user_message_id,
                run.assistant_message_id,
                run.workflow_id,
                run.task_id,
                run.error_code,
                run.error_message,
                run.created_at,
                run.updated_at,
                run.finished_at,
            ),
        )
        self._conn.commit()
        return run

    def get_run(self, run_id: str, *, tenant_id: str) -> ChatRun | None:
        row = self._conn.execute(
            "SELECT * FROM chat_runs WHERE run_id = ? AND tenant_id = ?",
            (run_id, tenant_id),
        ).fetchone()
        return self._run_row(row) if row else None

    def get_run_by_idempotency(
        self, *, tenant_id: str, conversation_id: str, idempotency_key: str
    ) -> ChatRun | None:
        row = self._conn.execute(
            """
            SELECT * FROM chat_runs
            WHERE tenant_id = ? AND conversation_id = ? AND idempotency_key = ?
            """,
            (tenant_id, conversation_id, idempotency_key),
        ).fetchone()
        return self._run_row(row) if row else None

    def list_runs_for_conversation(self, conversation_id: str, *, tenant_id: str) -> list[ChatRun]:
        rows = self._conn.execute(
            "SELECT * FROM chat_runs WHERE conversation_id = ? AND tenant_id = ? ORDER BY created_at",
            (conversation_id, tenant_id),
        ).fetchall()
        return [self._run_row(r) for r in rows]

    def save_transcript(self, t: VoiceTranscript) -> VoiceTranscript:
        self._conn.execute(
            """
            INSERT INTO chat_transcripts
            (transcript_id, tenant_id, user_id, text, audio_attachment_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (t.transcript_id, t.tenant_id, t.user_id, t.text, t.audio_attachment_id, t.created_at),
        )
        self._conn.commit()
        return t

    def save_voice_artifact(self, a: VoiceAudioArtifact, *, blob: bytes) -> VoiceAudioArtifact:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO chat_voice_artifacts
            (artifact_id, tenant_id, message_id, message_version, mime_type,
             byte_size, content_hash, created_at, blob)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                a.artifact_id,
                a.tenant_id,
                a.message_id,
                a.message_version,
                a.mime_type,
                a.byte_size,
                a.content_hash,
                a.created_at,
                blob,
            ),
        )
        self._conn.commit()
        return a

    def get_voice_artifact(self, artifact_id: str, *, tenant_id: str) -> VoiceAudioArtifact | None:
        row = self._conn.execute(
            """
            SELECT artifact_id, tenant_id, message_id, message_version, mime_type,
                   byte_size, content_hash, created_at
            FROM chat_voice_artifacts WHERE artifact_id = ? AND tenant_id = ?
            """,
            (artifact_id, tenant_id),
        ).fetchone()
        if not row:
            return None
        return VoiceAudioArtifact(
            artifact_id=row["artifact_id"],
            tenant_id=row["tenant_id"],
            message_id=row["message_id"],
            message_version=int(row["message_version"]),
            mime_type=row["mime_type"],
            byte_size=int(row["byte_size"]),
            content_hash=row["content_hash"],
            created_at=row["created_at"],
        )

    def get_voice_artifact_blob(self, artifact_id: str, *, tenant_id: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT blob FROM chat_voice_artifacts WHERE artifact_id = ? AND tenant_id = ?",
            (artifact_id, tenant_id),
        ).fetchone()
        return bytes(row["blob"]) if row else None

    def find_voice_artifact(
        self, *, tenant_id: str, message_id: str, message_version: int
    ) -> VoiceAudioArtifact | None:
        row = self._conn.execute(
            """
            SELECT artifact_id, tenant_id, message_id, message_version, mime_type,
                   byte_size, content_hash, created_at
            FROM chat_voice_artifacts
            WHERE tenant_id = ? AND message_id = ? AND message_version = ?
            """,
            (tenant_id, message_id, message_version),
        ).fetchone()
        if not row:
            return None
        return VoiceAudioArtifact(
            artifact_id=row["artifact_id"],
            tenant_id=row["tenant_id"],
            message_id=row["message_id"],
            message_version=int(row["message_version"]),
            mime_type=row["mime_type"],
            byte_size=int(row["byte_size"]),
            content_hash=row["content_hash"],
            created_at=row["created_at"],
        )

    def save_task(self, task: BackgroundTaskView) -> BackgroundTaskView:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO chat_background_tasks
            (task_id, tenant_id, user_id, conversation_id, run_id, operation_label,
             status, phase, progress_current, progress_total, result_artifact_ids,
             error_code, error_message, cancel_available, created_at, started_at,
             finished_at, workflow_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                task.tenant_id,
                task.user_id,
                task.conversation_id,
                task.run_id,
                task.operation_label,
                task.status,
                task.phase,
                task.progress_current,
                task.progress_total,
                json.dumps(list(task.result_artifact_ids)),
                task.error_code,
                task.error_message,
                1 if task.cancel_available else 0,
                task.created_at,
                task.started_at,
                task.finished_at,
                task.workflow_id,
            ),
        )
        self._conn.commit()
        return task

    def get_task(self, task_id: str, *, tenant_id: str) -> BackgroundTaskView | None:
        row = self._conn.execute(
            "SELECT * FROM chat_background_tasks WHERE task_id = ? AND tenant_id = ?",
            (task_id, tenant_id),
        ).fetchone()
        return self._task_row(row) if row else None

    def list_tasks(self, *, tenant_id: str, user_id: str, limit: int = 50) -> list[BackgroundTaskView]:
        rows = self._conn.execute(
            """
            SELECT * FROM chat_background_tasks
            WHERE tenant_id = ? AND user_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (tenant_id, user_id, limit),
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _conv_row(row) -> ChatConversation:
        return ChatConversation(
            conversation_id=row["conversation_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            title=row["title"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _msg_row(row) -> ChatMessage:
        return ChatMessage(
            message_id=row["message_id"],
            conversation_id=row["conversation_id"],
            tenant_id=row["tenant_id"],
            role=row["role"],
            content=row["content"],
            content_version=int(row["content_version"]),
            attachment_ids=tuple(json.loads(row["attachment_ids"] or "[]")),
            metadata_safe=json.loads(row["metadata_safe"] or "{}"),
            created_at=row["created_at"],
        )

    @staticmethod
    def _attach_row(row) -> AttachmentRef:
        return AttachmentRef(
            attachment_id=row["attachment_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            conversation_id=row["conversation_id"],
            filename_safe=row["filename_safe"],
            attachment_class=row["attachment_class"],
            mime_type=row["mime_type"],
            size_bytes=int(row["size_bytes"]),
            status=row["status"],
            artifact_ref=row["artifact_ref"],
            content_hash=row["content_hash"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _run_row(row) -> ChatRun:
        return ChatRun(
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            user_message_id=row["user_message_id"],
            assistant_message_id=row["assistant_message_id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _task_row(row) -> BackgroundTaskView:
        return BackgroundTaskView(
            task_id=row["task_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            conversation_id=row["conversation_id"],
            run_id=row["run_id"],
            operation_label=row["operation_label"],
            status=row["status"],
            phase=row["phase"],
            progress_current=row["progress_current"],
            progress_total=row["progress_total"],
            result_artifact_ids=tuple(json.loads(row["result_artifact_ids"] or "[]")),
            error_code=row["error_code"],
            error_message=row["error_message"],
            cancel_available=bool(row["cancel_available"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            workflow_id=row["workflow_id"],
        )
