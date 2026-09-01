"""SQLite persistence for Telegram interface transport state."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from telegram_interface.models import CallbackToken, ChatSession, TelegramBinding


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteTelegramInterfaceStore:
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
            CREATE TABLE IF NOT EXISTS tgi_bindings (
                binding_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                telegram_user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tgi_bind_user
                ON tgi_bindings(tenant_id, telegram_user_id, chat_id);

            CREATE TABLE IF NOT EXISTS tgi_processed_updates (
                update_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                processed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tgi_sessions (
                chat_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                active_request_id TEXT NOT NULL DEFAULT '',
                progress_message_id TEXT NOT NULL DEFAULT '',
                last_event_cursor TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS tgi_callbacks (
                token TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                action TEXT NOT NULL,
                approval_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self._conn.commit()

    def save_binding(self, binding: TelegramBinding) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO tgi_bindings
            (binding_id, tenant_id, owner_id, telegram_user_id, chat_id, conversation_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding.binding_id,
                binding.tenant_id,
                binding.owner_id,
                binding.telegram_user_id,
                binding.chat_id,
                binding.conversation_id,
                binding.status,
                binding.created_at,
                binding.updated_at,
            ),
        )
        self._conn.commit()

    def get_binding(
        self, *, tenant_id: str, telegram_user_id: str, chat_id: str
    ) -> TelegramBinding | None:
        row = self._conn.execute(
            """
            SELECT * FROM tgi_bindings
            WHERE tenant_id = ? AND telegram_user_id = ? AND chat_id = ? AND status = 'active'
            """,
            (tenant_id, telegram_user_id, chat_id),
        ).fetchone()
        return self._row_binding(row) if row else None

    def get_binding_by_chat(self, *, tenant_id: str, chat_id: str) -> TelegramBinding | None:
        row = self._conn.execute(
            "SELECT * FROM tgi_bindings WHERE tenant_id = ? AND chat_id = ? AND status = 'active' LIMIT 1",
            (tenant_id, chat_id),
        ).fetchone()
        return self._row_binding(row) if row else None

    def has_processed_update(self, update_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM tgi_processed_updates WHERE update_id = ?", (update_id,)
        ).fetchone()
        return row is not None

    def mark_processed_update(self, *, update_id: str, tenant_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO tgi_processed_updates (update_id, tenant_id, processed_at) VALUES (?, ?, ?)",
            (update_id, tenant_id, _utc_iso()),
        )
        self._conn.commit()

    def get_session(self, chat_id: str) -> ChatSession | None:
        row = self._conn.execute("SELECT * FROM tgi_sessions WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            return None
        return ChatSession(
            chat_id=row["chat_id"],
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            conversation_id=row["conversation_id"],
            active_request_id=row["active_request_id"] or "",
            progress_message_id=row["progress_message_id"] or "",
            last_event_cursor=row["last_event_cursor"] or "",
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def save_session(self, session: ChatSession) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO tgi_sessions
            (chat_id, tenant_id, owner_id, conversation_id, active_request_id, progress_message_id, last_event_cursor, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.chat_id,
                session.tenant_id,
                session.owner_id,
                session.conversation_id,
                session.active_request_id,
                session.progress_message_id,
                session.last_event_cursor,
                json.dumps(session.metadata),
            ),
        )
        self._conn.commit()

    def save_callback(self, cb: CallbackToken) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO tgi_callbacks
            (token, tenant_id, owner_id, request_id, action, approval_id, created_at, consumed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cb.token,
                cb.tenant_id,
                cb.owner_id,
                cb.request_id,
                cb.action,
                cb.approval_id,
                cb.created_at,
                1 if cb.consumed else 0,
            ),
        )
        self._conn.commit()

    def get_callback(self, token: str) -> CallbackToken | None:
        row = self._conn.execute("SELECT * FROM tgi_callbacks WHERE token = ?", (token,)).fetchone()
        if not row:
            return None
        return CallbackToken(
            token=row["token"],
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            request_id=row["request_id"],
            action=row["action"],
            approval_id=row["approval_id"] or "",
            created_at=row["created_at"],
            consumed=bool(row["consumed"]),
        )

    def consume_callback(self, token: str) -> bool:
        cur = self._conn.execute(
            "UPDATE tgi_callbacks SET consumed = 1 WHERE token = ? AND consumed = 0", (token,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def create_binding(
        self, *, tenant_id: str, owner_id: str, telegram_user_id: str, chat_id: str, conversation_id: str = ""
    ) -> TelegramBinding:
        now = _utc_iso()
        binding = TelegramBinding(
            binding_id=f"tgi_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            owner_id=owner_id,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            conversation_id=conversation_id,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.save_binding(binding)
        return binding

    @staticmethod
    def _row_binding(row: sqlite3.Row) -> TelegramBinding:
        return TelegramBinding(
            binding_id=row["binding_id"],
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            telegram_user_id=row["telegram_user_id"],
            chat_id=row["chat_id"],
            conversation_id=row["conversation_id"] or "",
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
