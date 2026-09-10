"""Encrypted storage for the owner's Telegram user session.

A user-account session authenticates AS THE OWNER: anyone holding it can
read everything that account can read. It is therefore never held in
plaintext, never returned by any API surface, and never logged. Storage
reuses the existing AES-256-GCM service in ``security.encryption`` rather
than adding another crypto path.
"""

from __future__ import annotations

from security.encryption import (
    DecryptionError,
    EncryptionService,
    EncryptionUnavailableError,
)

from market_intel.errors import (
    MI_SESSION_ENCRYPTION_REQUIRED,
    MI_SESSION_MISSING,
    MarketIntelError,
)


class TelegramSessionVault:
    def __init__(self, store, encryption: EncryptionService | None = None):
        self._store = store
        self._encryption = encryption

    def __repr__(self) -> str:
        return "TelegramSessionVault(sessions=[REDACTED])"

    def _crypto(self) -> EncryptionService:
        if self._encryption is None:
            self._encryption = EncryptionService.from_env()
        return self._encryption

    def store_session(
        self, *, tenant_id: str, owner_id: str, session_string: str, actor_id: str = ""
    ) -> dict:
        raw = str(session_string or "").strip()
        if not raw:
            raise MarketIntelError(MI_SESSION_MISSING, "empty Telegram user session", http_status=400)
        try:
            payload = self._crypto().encrypt(raw)
        except EncryptionUnavailableError as exc:
            raise MarketIntelError(
                MI_SESSION_ENCRYPTION_REQUIRED,
                "PANDA_ENCRYPTION_KEY is required to store a Telegram user session",
                http_status=503,
            ) from exc
        self._store.save_encrypted_session(
            tenant_id=tenant_id, owner_id=owner_id, encrypted_session=payload.serialize()
        )
        self._store.append_audit(
            actor_id=actor_id or owner_id,
            tenant_id=tenant_id,
            action="session.store",
            detail=f"owner={owner_id}",
        )
        return {"status": "stored", "tenant_id": tenant_id, "owner_id": owner_id}

    def has_session(self, *, tenant_id: str, owner_id: str) -> bool:
        return bool(self._store.get_encrypted_session(tenant_id=tenant_id, owner_id=owner_id))

    def load_session(self, *, tenant_id: str, owner_id: str) -> str:
        stored = self._store.get_encrypted_session(tenant_id=tenant_id, owner_id=owner_id)
        if not stored:
            raise MarketIntelError(
                MI_SESSION_MISSING, "no stored Telegram user session for this owner", http_status=403
            )
        try:
            return self._crypto().decrypt(stored)
        except EncryptionUnavailableError as exc:
            raise MarketIntelError(
                MI_SESSION_ENCRYPTION_REQUIRED,
                "PANDA_ENCRYPTION_KEY is required to read a Telegram user session",
                http_status=503,
            ) from exc
        except DecryptionError as exc:
            raise MarketIntelError(
                MI_SESSION_MISSING, "stored Telegram user session could not be decrypted", http_status=403
            ) from exc

    def revoke(self, *, tenant_id: str, owner_id: str, actor_id: str = "") -> dict:
        self._store.revoke_session(tenant_id=tenant_id, owner_id=owner_id)
        self._store.append_audit(
            actor_id=actor_id or owner_id,
            tenant_id=tenant_id,
            action="session.revoke",
            detail=f"owner={owner_id}",
        )
        return {"status": "revoked", "tenant_id": tenant_id, "owner_id": owner_id}
