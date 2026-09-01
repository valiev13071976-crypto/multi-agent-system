"""SQLite persistence for Business Assistant API — durable request/event state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from business_assistant_api.models import (
    ApiRequestRecord,
    ConversationRecord,
    MessageRecord,
    ProgressEvent,
)


class SqliteBusinessAssistantApiStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ba_api_requests (
                request_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL DEFAULT '',
                payload_hash TEXT NOT NULL DEFAULT '',
                ba_request_id TEXT NOT NULL DEFAULT '',
                plan_id TEXT NOT NULL DEFAULT '',
                execution_id TEXT NOT NULL DEFAULT '',
                workflow_id TEXT NOT NULL DEFAULT '',
                correlation_id TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT '',
                workload_class TEXT NOT NULL DEFAULT 'interactive',
                artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                read_only INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                finops_cost TEXT NOT NULL DEFAULT '0',
                approval_id TEXT NOT NULL DEFAULT '',
                preview_id TEXT NOT NULL DEFAULT '',
                plan_fingerprint TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ba_api_idem
                ON ba_api_requests(tenant_id, idempotency_key)
                WHERE idempotency_key != '';

            CREATE TABLE IF NOT EXISTS ba_api_events (
                event_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                workflow_id TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT '',
                step TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                correlation_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ba_api_events_req
                ON ba_api_events(request_id, timestamp);

            CREATE TABLE IF NOT EXISTS ba_api_conversations (
                conversation_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_ba_api_conv_tenant
                ON ba_api_conversations(tenant_id, owner_id);

            CREATE TABLE IF NOT EXISTS ba_api_messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT '',
                artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ba_api_msg_conv
                ON ba_api_messages(conversation_id, created_at);

            CREATE TABLE IF NOT EXISTS ba_api_snapshots (
                request_id TEXT PRIMARY KEY,
                snapshot_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ba_api_artifacts (
                artifact_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                ref TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                content_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ba_api_art_req
                ON ba_api_artifacts(request_id, tenant_id);
            """
        )
        self._conn.commit()

    def save_request(self, rec: ApiRequestRecord) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO ba_api_requests (
                request_id, tenant_id, owner_id, status, message, conversation_id,
                idempotency_key, payload_hash, ba_request_id, plan_id, execution_id,
                workflow_id, correlation_id, trace_id, workload_class, artifact_refs_json,
                read_only, error_code, error_message, finops_cost, approval_id, preview_id,
                plan_fingerprint, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rec.request_id,
                rec.tenant_id,
                rec.owner_id,
                rec.status,
                rec.message,
                rec.conversation_id,
                rec.idempotency_key,
                rec.payload_hash,
                rec.ba_request_id,
                rec.plan_id,
                rec.execution_id,
                rec.workflow_id,
                rec.correlation_id,
                rec.trace_id,
                rec.workload_class,
                json.dumps(list(rec.artifact_refs)),
                1 if rec.read_only else 0,
                rec.error_code,
                rec.error_message,
                rec.finops_cost,
                rec.approval_id,
                rec.preview_id,
                rec.plan_fingerprint,
                rec.created_at,
                rec.updated_at,
            ),
        )
        self._conn.commit()

    def get_request(self, *, tenant_id: str, request_id: str) -> ApiRequestRecord | None:
        row = self._conn.execute(
            "SELECT * FROM ba_api_requests WHERE request_id=? AND tenant_id=?",
            (request_id, tenant_id),
        ).fetchone()
        return self._row_request(row) if row else None

    def get_request_by_idempotency(
        self, *, tenant_id: str, idempotency_key: str
    ) -> ApiRequestRecord | None:
        if not idempotency_key:
            return None
        row = self._conn.execute(
            "SELECT * FROM ba_api_requests WHERE tenant_id=? AND idempotency_key=?",
            (tenant_id, idempotency_key),
        ).fetchone()
        return self._row_request(row) if row else None

    def save_event(self, ev: ProgressEvent) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO ba_api_events (
                event_id, request_id, tenant_id, event_type, timestamp, workflow_id,
                stage, step, status, message, metadata_json, correlation_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ev.event_id,
                ev.request_id,
                ev.tenant_id,
                ev.event_type,
                ev.timestamp,
                ev.workflow_id,
                ev.stage,
                ev.step,
                ev.status,
                ev.message,
                json.dumps(ev.metadata),
                ev.correlation_id,
            ),
        )
        self._conn.commit()

    def list_events(
        self, *, tenant_id: str, request_id: str, limit: int = 200, after: str | None = None
    ) -> list[ProgressEvent]:
        q = "SELECT * FROM ba_api_events WHERE tenant_id=? AND request_id=?"
        params: list = [tenant_id, request_id]
        if after:
            q += " AND timestamp > ?"
            params.append(after)
        q += " ORDER BY timestamp ASC, event_id ASC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(q, params).fetchall()
        return [self._row_event(r) for r in rows]

    def save_snapshot(self, *, request_id: str, snapshot: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO ba_api_snapshots(request_id, snapshot_json) VALUES (?,?)",
            (request_id, json.dumps(snapshot, sort_keys=True)),
        )
        self._conn.commit()

    def load_snapshot(self, *, request_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT snapshot_json FROM ba_api_snapshots WHERE request_id=?", (request_id,)
        ).fetchone()
        if not row:
            return None
        return json.loads(row["snapshot_json"])

    def save_conversation(self, conv: ConversationRecord) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO ba_api_conversations
            (conversation_id, tenant_id, owner_id, created_at, updated_at, metadata_json)
            VALUES (?,?,?,?,?,?)
            """,
            (
                conv.conversation_id,
                conv.tenant_id,
                conv.owner_id,
                conv.created_at,
                conv.updated_at,
                json.dumps(conv.metadata),
            ),
        )
        self._conn.commit()

    def get_conversation(self, *, tenant_id: str, owner_id: str, conversation_id: str) -> ConversationRecord | None:
        row = self._conn.execute(
            "SELECT * FROM ba_api_conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
            (conversation_id, tenant_id, owner_id),
        ).fetchone()
        if not row:
            return None
        return ConversationRecord(
            conversation_id=row["conversation_id"],
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def save_message(self, msg: MessageRecord) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO ba_api_messages
            (message_id, conversation_id, tenant_id, role, content, request_id, artifact_refs_json, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                msg.message_id,
                msg.conversation_id,
                msg.tenant_id,
                msg.role,
                msg.content,
                msg.request_id,
                json.dumps(list(msg.artifact_refs)),
                msg.created_at,
            ),
        )
        self._conn.commit()

    def list_messages(self, *, tenant_id: str, conversation_id: str, limit: int = 100) -> list[MessageRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM ba_api_messages WHERE tenant_id=? AND conversation_id=?
            ORDER BY created_at ASC LIMIT ?
            """,
            (tenant_id, conversation_id, limit),
        ).fetchall()
        return [
            MessageRecord(
                message_id=r["message_id"],
                conversation_id=r["conversation_id"],
                tenant_id=r["tenant_id"],
                role=r["role"],
                content=r["content"],
                created_at=r["created_at"],
                request_id=r["request_id"] or "",
                artifact_refs=tuple(json.loads(r["artifact_refs_json"] or "[]")),
            )
            for r in rows
        ]

    def save_artifact(
        self,
        *,
        artifact_id: str,
        request_id: str,
        tenant_id: str,
        owner_id: str,
        artifact_type: str,
        ref: str = "",
        metadata: dict | None = None,
        content: dict | None = None,
        created_at: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO ba_api_artifacts
            (artifact_id, request_id, tenant_id, owner_id, artifact_type, ref, metadata_json, content_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                artifact_id,
                request_id,
                tenant_id,
                owner_id,
                artifact_type,
                ref,
                json.dumps(metadata or {}),
                json.dumps(content or {}),
                created_at,
            ),
        )
        self._conn.commit()

    def list_artifacts(self, *, tenant_id: str, request_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM ba_api_artifacts WHERE tenant_id=? AND request_id=? ORDER BY created_at",
            (tenant_id, request_id),
        ).fetchall()
        return [
            {
                "artifact_id": r["artifact_id"],
                "artifact_type": r["artifact_type"],
                "ref": r["ref"],
                "metadata": json.loads(r["metadata_json"] or "{}"),
                "content": json.loads(r["content_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def _row_request(self, row) -> ApiRequestRecord:
        return ApiRequestRecord(
            request_id=row["request_id"],
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            status=row["status"],
            message=row["message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            conversation_id=row["conversation_id"] or "",
            idempotency_key=row["idempotency_key"] or "",
            payload_hash=row["payload_hash"] or "",
            ba_request_id=row["ba_request_id"] or "",
            plan_id=row["plan_id"] or "",
            execution_id=row["execution_id"] or "",
            workflow_id=row["workflow_id"] or "",
            correlation_id=row["correlation_id"] or "",
            trace_id=row["trace_id"] or "",
            workload_class=row["workload_class"] or "interactive",
            artifact_refs=tuple(json.loads(row["artifact_refs_json"] or "[]")),
            read_only=bool(row["read_only"]),
            error_code=row["error_code"] or "",
            error_message=row["error_message"] or "",
            finops_cost=row["finops_cost"] or "0",
            approval_id=row["approval_id"] or "",
            preview_id=row["preview_id"] or "",
            plan_fingerprint=row["plan_fingerprint"] or "",
        )

    def _row_event(self, row) -> ProgressEvent:
        return ProgressEvent(
            event_id=row["event_id"],
            request_id=row["request_id"],
            tenant_id=row["tenant_id"],
            event_type=row["event_type"],
            timestamp=row["timestamp"],
            workflow_id=row["workflow_id"] or "",
            stage=row["stage"] or "",
            step=row["step"] or "",
            status=row["status"] or "",
            message=row["message"] or "",
            metadata=json.loads(row["metadata_json"] or "{}"),
            correlation_id=row["correlation_id"] or "",
        )
