"""Voice interface persistence."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from voice_interface.models import VoiceRequestRecord


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteVoiceInterfaceStore:
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
            CREATE TABLE IF NOT EXISTS vi_voice_requests (
                voice_request_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                ba_request_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                transcript TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL DEFAULT '',
                tts_artifact_id TEXT NOT NULL DEFAULT '',
                tts_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vi_idem
                ON vi_voice_requests(tenant_id, owner_id, idempotency_key)
                WHERE idempotency_key != '';

            CREATE TABLE IF NOT EXISTS vi_tts_artifacts (
                artifact_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                storage_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def get_by_idempotency(self, *, tenant_id: str, owner_id: str, idempotency_key: str) -> VoiceRequestRecord | None:
        if not idempotency_key:
            return None
        row = self._conn.execute(
            """
            SELECT * FROM vi_voice_requests
            WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ?
            """,
            (tenant_id, owner_id, idempotency_key),
        ).fetchone()
        return self._row(row) if row else None

    def get_by_ba_request(self, *, tenant_id: str, owner_id: str, ba_request_id: str) -> VoiceRequestRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM vi_voice_requests
            WHERE tenant_id = ? AND owner_id = ? AND ba_request_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (tenant_id, owner_id, ba_request_id),
        ).fetchone()
        return self._row(row) if row else None

    def save_voice_request(self, rec: VoiceRequestRecord) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO vi_voice_requests
            (voice_request_id, tenant_id, owner_id, ba_request_id, conversation_id, transcript,
             idempotency_key, tts_artifact_id, tts_error, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.voice_request_id,
                rec.tenant_id,
                rec.owner_id,
                rec.ba_request_id,
                rec.conversation_id,
                rec.transcript,
                rec.idempotency_key,
                rec.tts_artifact_id,
                rec.tts_error,
                rec.created_at,
                json.dumps(rec.metadata),
            ),
        )
        self._conn.commit()

    def save_tts_artifact(
        self, *, artifact_id: str, tenant_id: str, owner_id: str, mime_type: str, byte_size: int, storage_path: str
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO vi_tts_artifacts
            (artifact_id, tenant_id, owner_id, mime_type, byte_size, storage_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, tenant_id, owner_id, mime_type, byte_size, storage_path, _utc_iso()),
        )
        self._conn.commit()

    def get_tts_artifact(self, *, artifact_id: str, tenant_id: str, owner_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM vi_tts_artifacts WHERE artifact_id = ? AND tenant_id = ? AND owner_id = ?",
            (artifact_id, tenant_id, owner_id),
        ).fetchone()
        if not row:
            return None
        return {
            "artifact_id": row["artifact_id"],
            "tenant_id": row["tenant_id"],
            "owner_id": row["owner_id"],
            "mime_type": row["mime_type"],
            "byte_size": row["byte_size"],
            "storage_path": row["storage_path"],
        }

    def create_voice_request(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        ba_request_id: str,
        conversation_id: str,
        transcript: str,
        idempotency_key: str = "",
    ) -> VoiceRequestRecord:
        rec = VoiceRequestRecord(
            voice_request_id=f"vr_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            owner_id=owner_id,
            ba_request_id=ba_request_id,
            conversation_id=conversation_id,
            transcript=transcript,
            idempotency_key=idempotency_key,
            created_at=_utc_iso(),
        )
        self.save_voice_request(rec)
        return rec

    @staticmethod
    def _row(row: sqlite3.Row) -> VoiceRequestRecord:
        return VoiceRequestRecord(
            voice_request_id=row["voice_request_id"],
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            ba_request_id=row["ba_request_id"],
            conversation_id=row["conversation_id"],
            transcript=row["transcript"],
            idempotency_key=row["idempotency_key"] or "",
            tts_artifact_id=row["tts_artifact_id"] or "",
            tts_error=row["tts_error"] or "",
            created_at=row["created_at"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )
