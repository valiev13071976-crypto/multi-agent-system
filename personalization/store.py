"""Personalization persistence — one canonical row per (tenant_id,
owner_id) (Block 4.28.5: one canonical preference source, not duplicated
into every message record)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from personalization.models import UserPreferences


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqlitePersonalizationStore:
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
            CREATE TABLE IF NOT EXISTS pz_preferences (
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                style TEXT NOT NULL,
                tone TEXT NOT NULL DEFAULT '',
                length TEXT NOT NULL,
                language TEXT NOT NULL,
                voice_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, owner_id)
            );
            """
        )
        self._conn.commit()

    def get(self, *, tenant_id: str, owner_id: str) -> UserPreferences | None:
        row = self._conn.execute(
            "SELECT * FROM pz_preferences WHERE tenant_id = ? AND owner_id = ?",
            (tenant_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        return UserPreferences(
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            style=row["style"],
            tone=row["tone"] or "",
            length=row["length"],
            language=row["language"],
            voice_id=row["voice_id"],
            updated_at=row["updated_at"],
        )

    def upsert(self, prefs: UserPreferences) -> UserPreferences:
        now = _utc_iso()
        self._conn.execute(
            """
            INSERT INTO pz_preferences
                (tenant_id, owner_id, style, tone, length, language, voice_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, owner_id) DO UPDATE SET
                style = excluded.style,
                tone = excluded.tone,
                length = excluded.length,
                language = excluded.language,
                voice_id = excluded.voice_id,
                updated_at = excluded.updated_at
            """,
            (
                prefs.tenant_id,
                prefs.owner_id,
                prefs.style,
                prefs.tone,
                prefs.length,
                prefs.language,
                prefs.voice_id,
                now,
            ),
        )
        self._conn.commit()
        return UserPreferences(
            tenant_id=prefs.tenant_id,
            owner_id=prefs.owner_id,
            style=prefs.style,
            tone=prefs.tone,
            length=prefs.length,
            language=prefs.language,
            voice_id=prefs.voice_id,
            updated_at=now,
        )
