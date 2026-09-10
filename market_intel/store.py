"""SQLite persistence for Market Intelligence.

Additive only: every table is new and prefixed ``mi_``, created with
CREATE TABLE IF NOT EXISTS in this subsystem's own database file. No
existing table or schema is read, altered or migrated here.

Prices are stored as TEXT and rehydrated through ``Decimal`` so an
observed market price never passes through a binary float.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from market_intel.models import (
    MONITOR_ENABLED,
    MONITOR_PENDING,
    MarketObservation,
    MonitoredChannel,
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec(raw: object) -> Decimal | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (ArithmeticError, ValueError):
        return None


def _price_text(value: Decimal | None) -> str:
    return "" if value is None else str(value)


class SqliteMarketIntelStore:
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
            CREATE TABLE IF NOT EXISTS mi_channels (
                channel_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                dialog_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'unknown',
                monitor_state TEXT NOT NULL DEFAULT 'pending',
                last_message_id TEXT NOT NULL DEFAULT '',
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mi_channel_dialog
                ON mi_channels(tenant_id, dialog_id);

            CREATE TABLE IF NOT EXISTS mi_observations (
                observation_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                dialog_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                brand TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                ean TEXT NOT NULL DEFAULT '',
                price TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT '',
                matched_product_id TEXT NOT NULL DEFAULT '',
                match_state TEXT NOT NULL DEFAULT '',
                match_method TEXT NOT NULL DEFAULT '',
                source_link TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mi_obs_message
                ON mi_observations(tenant_id, channel_id, message_id);
            CREATE INDEX IF NOT EXISTS idx_mi_obs_product
                ON mi_observations(tenant_id, matched_product_id);

            CREATE TABLE IF NOT EXISTS mi_user_sessions (
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                encrypted_session TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, owner_id)
            );

            CREATE TABLE IF NOT EXISTS mi_channel_audit (
                event_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                action TEXT NOT NULL,
                channel_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT 'ok'
            );
            """
        )
        self._conn.commit()

    # ---------------- channels ----------------

    def upsert_channel(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        dialog_id: str,
        title: str,
        username: str,
        kind: str,
    ) -> tuple[MonitoredChannel, bool]:
        """Idempotent discovery upsert. Returns ``(channel, is_new)``.

        Re-discovery refreshes only the descriptive fields; it never
        resets an owner's monitoring decision.
        """
        existing = self.get_channel_by_dialog(tenant_id=tenant_id, dialog_id=dialog_id)
        now = _utc_iso()
        if existing is not None:
            self._conn.execute(
                "UPDATE mi_channels SET title=?, username=?, kind=?, updated_at=? WHERE channel_id=?",
                (title, username, kind, now, existing.channel_id),
            )
            self._conn.commit()
            return (self.get_channel(tenant_id=tenant_id, channel_id=existing.channel_id), False)
        channel_id = f"mic_{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            """
            INSERT INTO mi_channels
            (channel_id, tenant_id, owner_id, dialog_id, title, username, kind,
             monitor_state, last_message_id, discovered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (channel_id, tenant_id, owner_id, dialog_id, title, username, kind, MONITOR_PENDING, now, now),
        )
        self._conn.commit()
        return (self.get_channel(tenant_id=tenant_id, channel_id=channel_id), True)

    def get_channel(self, *, tenant_id: str, channel_id: str) -> MonitoredChannel | None:
        row = self._conn.execute(
            "SELECT * FROM mi_channels WHERE tenant_id=? AND channel_id=?", (tenant_id, channel_id)
        ).fetchone()
        return self._row_channel(row) if row else None

    def get_channel_by_dialog(self, *, tenant_id: str, dialog_id: str) -> MonitoredChannel | None:
        row = self._conn.execute(
            "SELECT * FROM mi_channels WHERE tenant_id=? AND dialog_id=?", (tenant_id, str(dialog_id))
        ).fetchone()
        return self._row_channel(row) if row else None

    def list_channels(self, *, tenant_id: str, monitor_state: str = "") -> list[MonitoredChannel]:
        if monitor_state:
            rows = self._conn.execute(
                "SELECT * FROM mi_channels WHERE tenant_id=? AND monitor_state=? ORDER BY discovered_at ASC",
                (tenant_id, monitor_state),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM mi_channels WHERE tenant_id=? ORDER BY discovered_at ASC", (tenant_id,)
            ).fetchall()
        return [self._row_channel(r) for r in rows]

    def set_monitor_state(self, *, tenant_id: str, channel_id: str, monitor_state: str) -> None:
        self._conn.execute(
            "UPDATE mi_channels SET monitor_state=?, updated_at=? WHERE tenant_id=? AND channel_id=?",
            (monitor_state, _utc_iso(), tenant_id, channel_id),
        )
        self._conn.commit()

    def set_last_message_id(self, *, tenant_id: str, channel_id: str, last_message_id: str) -> None:
        self._conn.execute(
            "UPDATE mi_channels SET last_message_id=?, updated_at=? WHERE tenant_id=? AND channel_id=?",
            (str(last_message_id), _utc_iso(), tenant_id, channel_id),
        )
        self._conn.commit()

    # ---------------- observations ----------------

    def save_observation(self, observation: MarketObservation) -> bool:
        """Insert one observation. Returns False when this exact message was
        already observed (re-reading a channel must not duplicate history)."""
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO mi_observations
            (observation_id, tenant_id, channel_id, dialog_id, message_id, observed_at,
             title, brand, model, ean, price, currency,
             matched_product_id, match_state, match_method, source_link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.observation_id,
                observation.tenant_id,
                observation.channel_id,
                observation.dialog_id,
                observation.message_id,
                observation.observed_at,
                observation.title,
                observation.brand,
                observation.model,
                observation.ean,
                _price_text(observation.price),
                observation.currency,
                observation.matched_product_id,
                observation.match_state,
                observation.match_method,
                observation.source_link,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_observations(
        self, *, tenant_id: str, product_id: str = "", channel_id: str = ""
    ) -> list[MarketObservation]:
        sql = "SELECT * FROM mi_observations WHERE tenant_id=?"
        params: list[str] = [tenant_id]
        if product_id:
            sql += " AND matched_product_id=?"
            params.append(product_id)
        if channel_id:
            sql += " AND channel_id=?"
            params.append(channel_id)
        sql += " ORDER BY observed_at ASC, message_id ASC"
        return [self._row_observation(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    # ---------------- encrypted user session ----------------

    def save_encrypted_session(self, *, tenant_id: str, owner_id: str, encrypted_session: str) -> None:
        now = _utc_iso()
        self._conn.execute(
            """
            INSERT INTO mi_user_sessions (tenant_id, owner_id, encrypted_session, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            ON CONFLICT(tenant_id, owner_id) DO UPDATE SET
                encrypted_session=excluded.encrypted_session, status='active', updated_at=excluded.updated_at
            """,
            (tenant_id, owner_id, encrypted_session, now, now),
        )
        self._conn.commit()

    def get_encrypted_session(self, *, tenant_id: str, owner_id: str) -> str:
        row = self._conn.execute(
            "SELECT encrypted_session FROM mi_user_sessions WHERE tenant_id=? AND owner_id=? AND status='active'",
            (tenant_id, owner_id),
        ).fetchone()
        return str(row["encrypted_session"]) if row else ""

    def revoke_session(self, *, tenant_id: str, owner_id: str) -> None:
        self._conn.execute(
            "UPDATE mi_user_sessions SET encrypted_session='', status='revoked', updated_at=? "
            "WHERE tenant_id=? AND owner_id=?",
            (_utc_iso(), tenant_id, owner_id),
        )
        self._conn.commit()

    # ---------------- audit ----------------

    def append_audit(
        self, *, actor_id: str, tenant_id: str, action: str, channel_id: str = "", detail: str = "", result: str = "ok"
    ) -> dict:
        event_id = f"mia_{uuid.uuid4().hex[:12]}"
        ts = _utc_iso()
        self._conn.execute(
            """
            INSERT INTO mi_channel_audit
            (event_id, timestamp, actor_id, tenant_id, action, channel_id, detail, result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, ts, actor_id, tenant_id, action, channel_id, detail, result),
        )
        self._conn.commit()
        return {
            "event_id": event_id,
            "timestamp": ts,
            "actor_id": actor_id,
            "tenant_id": tenant_id,
            "action": action,
            "channel_id": channel_id,
            "detail": detail,
            "result": result,
        }

    def list_audit(self, *, tenant_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM mi_channel_audit WHERE tenant_id=? ORDER BY timestamp ASC", (tenant_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- row mapping ----------------

    @staticmethod
    def _row_channel(row: sqlite3.Row) -> MonitoredChannel:
        return MonitoredChannel(
            channel_id=row["channel_id"],
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            dialog_id=row["dialog_id"],
            title=row["title"] or "",
            username=row["username"] or "",
            kind=row["kind"] or "",
            monitor_state=row["monitor_state"] or MONITOR_PENDING,
            last_message_id=row["last_message_id"] or "",
            discovered_at=row["discovered_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_observation(row: sqlite3.Row) -> MarketObservation:
        return MarketObservation(
            observation_id=row["observation_id"],
            tenant_id=row["tenant_id"],
            channel_id=row["channel_id"],
            dialog_id=row["dialog_id"],
            message_id=row["message_id"],
            observed_at=row["observed_at"],
            title=row["title"] or "",
            brand=row["brand"] or "",
            model=row["model"] or "",
            ean=row["ean"] or "",
            price=_dec(row["price"]),
            currency=row["currency"] or "",
            matched_product_id=row["matched_product_id"] or "",
            match_state=row["match_state"] or "",
            match_method=row["match_method"] or "",
            source_link=row["source_link"] or "",
        )


__all__ = ["SqliteMarketIntelStore", "MONITOR_ENABLED", "MONITOR_PENDING"]
