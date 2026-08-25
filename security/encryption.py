from __future__ import annotations

import base64
import json
import os
import secrets as pysecrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from security.secrets import SecretProvider


PAYLOAD_VERSION = 1
ALGORITHM = "AESGCM"
DEFAULT_KEY_ID = "v1"
ENCRYPTION_KEY_ENV = "PANDA_ENCRYPTION_KEY"
ENCRYPTION_KEY_ID_ENV = "PANDA_ENCRYPTION_KEY_ID"
SENSITIVITY_PUBLIC = "public"
SENSITIVITY_INTERNAL = "internal"
SENSITIVITY_SENSITIVE = "sensitive"
SENSITIVITY_SECRET = "secret"
ENCRYPTION_REQUIRED = frozenset({SENSITIVITY_SENSITIVE, SENSITIVITY_SECRET})


class EncryptionUnavailableError(RuntimeError):
    pass


class InvalidEncryptionKeyError(ValueError):
    pass


class DecryptionError(ValueError):
    pass


def _decode_key(raw: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as exc:
        raise InvalidEncryptionKeyError(
            "PANDA_ENCRYPTION_KEY must be urlsafe base64 of 32 bytes."
        ) from exc
    if len(key) != 32:
        raise InvalidEncryptionKeyError(
            "PANDA_ENCRYPTION_KEY must decode to exactly 32 bytes."
        )
    return key


def load_encryption_key(secrets: SecretProvider | None = None) -> bytes | None:
    provider = secrets or SecretProvider()
    raw = provider.get(ENCRYPTION_KEY_ENV)
    if raw is None:
        return None
    return _decode_key(raw)


@dataclass(frozen=True)
class EncryptedPayload:
    version: int
    key_id: str
    algorithm: str
    nonce_b64: str
    ciphertext_b64: str

    def to_dict(self) -> dict:
        return {
            "v": self.version,
            "kid": self.key_id,
            "alg": self.algorithm,
            "n": self.nonce_b64,
            "ct": self.ciphertext_b64,
        }

    def serialize(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> EncryptedPayload:
        return cls(
            version=int(data["v"]),
            key_id=str(data["kid"]),
            algorithm=str(data["alg"]),
            nonce_b64=str(data["n"]),
            ciphertext_b64=str(data["ct"]),
        )

    @classmethod
    def deserialize(cls, raw: str | EncryptedPayload) -> EncryptedPayload:
        if isinstance(raw, EncryptedPayload):
            return raw
        return cls.from_dict(json.loads(raw))


class EncryptionService:
    """
    AES-256-GCM authenticated encryption.
    Nonce is generated per encrypt. Payload is versioned and rotation-ready.
    """

    def __init__(
        self,
        *,
        key: bytes | None = None,
        key_id: str | None = None,
        keyring: dict[str, bytes] | None = None,
        require_key: bool = True,
    ):
        if keyring is None:
            keyring = {}
            if key is not None:
                keyring[key_id or DEFAULT_KEY_ID] = key
        self._keyring = dict(keyring)
        self._active_key_id = key_id or DEFAULT_KEY_ID
        self._require_key = require_key

    @classmethod
    def from_env(cls, secrets: SecretProvider | None = None) -> EncryptionService:
        provider = secrets or SecretProvider()
        raw = provider.get(ENCRYPTION_KEY_ENV)
        key_id = provider.get(ENCRYPTION_KEY_ID_ENV) or DEFAULT_KEY_ID
        if raw is None:
            return cls(key=None, key_id=key_id, require_key=True)
        return cls(key=_decode_key(raw), key_id=key_id, require_key=True)

    def _active_key(self) -> bytes:
        key = self._keyring.get(self._active_key_id)
        if key is None:
            raise EncryptionUnavailableError(
                "Encryption key is not configured for an encryption-required operation."
            )
        return key

    def encrypt(self, plaintext: str) -> EncryptedPayload:
        key = self._active_key()
        nonce = pysecrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return EncryptedPayload(
            version=PAYLOAD_VERSION,
            key_id=self._active_key_id,
            algorithm=ALGORITHM,
            nonce_b64=base64.urlsafe_b64encode(nonce).decode("ascii"),
            ciphertext_b64=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        )

    def decrypt(self, payload: EncryptedPayload | str) -> str:
        parsed = EncryptedPayload.deserialize(payload)
        key = self._keyring.get(parsed.key_id)
        if key is None:
            raise EncryptionUnavailableError(
                "No encryption key is available for this payload key id."
            )
        try:
            nonce = base64.urlsafe_b64decode(parsed.nonce_b64.encode("ascii"))
            ciphertext = base64.urlsafe_b64decode(parsed.ciphertext_b64.encode("ascii"))
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        except (InvalidTag, ValueError, KeyError) as exc:
            raise DecryptionError("Unable to decrypt payload.") from exc
        return plaintext.decode("utf-8")


class EncryptedStore:
    """
    Write-through store that encrypts sensitive/secret values.
    Not wired to DecisionMemory in this patch.
    """

    def __init__(self, encryption: EncryptionService):
        self._encryption = encryption
        self._items: dict[str, tuple[str, object]] = {}

    def put(self, key: str, value: str, sensitivity: str) -> None:
        if sensitivity in ENCRYPTION_REQUIRED:
            self._items[key] = ("encrypted", self._encryption.encrypt(value))
            return
        self._items[key] = ("plaintext", value)

    def get(self, key: str) -> str:
        kind, payload = self._items[key]
        if kind == "encrypted":
            return self._encryption.decrypt(payload)
        return str(payload)
