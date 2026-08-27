"""Inbound webhook foundation — verify, dedupe, normalize; no business side effects."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from autonomy.models import sanitize_metadata
from integrations.contracts import WebhookEnvelope
from integrations.errors import (
    IpAllowlistDeniedError,
    WebhookReplayError,
    WebhookSignatureInvalidError,
)
from security.tenant import normalize_tenant_id

_DDL = """
CREATE TABLE IF NOT EXISTS integration_webhook_events (
    tenant_id TEXT NOT NULL,
    integration_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    received_at TEXT NOT NULL,
    signature_verified INTEGER NOT NULL,
    payload_ref TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, integration_id, event_id)
);
"""


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class WebhookProcessor:
    def __init__(
        self,
        *,
        secrets_backend,
        path: str | None = None,
        shared_connection=None,
        timestamp_tolerance_seconds: float = 300.0,
    ):
        self._secrets = secrets_backend
        self._shared = shared_connection
        self._path = path or ":memory:"
        self._tolerance = float(timestamp_tolerance_seconds)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._owns = shared_connection is None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared.connect()
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _commit(self, conn) -> None:
        if self._shared is not None and hasattr(self._shared, "maybe_autocommit"):
            self._shared.maybe_autocommit()
            return
        conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_DDL)
            self._commit(conn)

    def verify_hmac(
        self,
        *,
        secret: str,
        body: bytes,
        signature_header: str,
        algorithm: str = "sha256",
    ) -> bool:
        if not secret or not signature_header:
            return False
        sig = signature_header.strip()
        if sig.startswith("sha256="):
            sig = sig[7:]
        digest = hmac.new(secret.encode("utf-8"), body, getattr(hashlib, algorithm)).hexdigest()
        return hmac.compare_digest(digest, sig)

    def process(
        self,
        *,
        tenant_id: str,
        integration_id: str,
        provider: str,
        event_id: str,
        event_type: str,
        body: bytes,
        signature_header: str = "",
        secret_ref: str = "",
        occurred_at: datetime | None = None,
        source_ip: str | None = None,
        ip_allowlist: tuple[str, ...] = (),
        require_signature: bool = True,
        # IP alone must never authenticate
        allow_ip_only_auth: bool = False,
    ) -> WebhookEnvelope:
        if allow_ip_only_auth:
            raise WebhookSignatureInvalidError("webhook_signature_invalid")
        tenant = normalize_tenant_id(tenant_id)
        if ip_allowlist:
            if not source_ip or source_ip not in ip_allowlist:
                raise IpAllowlistDeniedError("ip_allowlist_denied")

        if occurred_at is not None:
            skew = abs((_utc() - occurred_at).total_seconds())
            if skew > self._tolerance:
                raise WebhookReplayError("webhook_replay")

        verified = False
        if require_signature:
            if not secret_ref:
                raise WebhookSignatureInvalidError("webhook_signature_invalid")
            secret = self._secrets.get_secret(tenant_id=tenant, secret_ref=secret_ref)
            if not secret:
                raise WebhookSignatureInvalidError("webhook_signature_invalid")
            verified = self.verify_hmac(
                secret=secret, body=body, signature_header=signature_header
            )
            if not verified:
                raise WebhookSignatureInvalidError("webhook_signature_invalid")

        with self._lock:
            conn = self._connect()
            existing = conn.execute(
                "SELECT event_id FROM integration_webhook_events "
                "WHERE tenant_id=? AND integration_id=? AND event_id=?",
                (tenant, integration_id, event_id),
            ).fetchone()
            if existing is not None:
                raise WebhookReplayError("webhook_replay")

            payload_ref = hashlib.sha256(body).hexdigest()
            # Persist metadata only — not raw sensitive payload
            meta = sanitize_metadata(
                {"body_sha256": payload_ref, "body_bytes": len(body), "provider": provider}
            )
            now = _utc()
            conn.execute(
                "INSERT INTO integration_webhook_events("
                "tenant_id, integration_id, event_id, event_type, provider, received_at, "
                "signature_verified, payload_ref, metadata_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    integration_id,
                    event_id,
                    event_type,
                    provider,
                    now.isoformat(),
                    1 if verified else 0,
                    payload_ref,
                    json.dumps(meta, separators=(",", ":"), sort_keys=True),
                ),
            )
            self._commit(conn)

        return WebhookEnvelope(
            provider=provider,
            integration_id=integration_id,
            tenant_id=tenant,
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            received_at=now,
            payload_ref=payload_ref,
            signature_verified=verified,
            idempotency_status="accepted",
            normalized_metadata=meta,
        )

    def close(self) -> None:
        if not self._owns:
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
