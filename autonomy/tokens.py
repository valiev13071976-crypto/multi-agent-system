import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from autonomy.capabilities import CapabilityScope, scope_mismatch
from autonomy.models import utc_now
from security.secrets import SecretProvider, SecretStore


SIGNING_KEY_ENV = "PANDA_CAPABILITY_SIGNING_KEY"
DEFAULT_KID = "cap-v1"


class TokenSigner:
    kid = DEFAULT_KID

    def sign(self, payload: str) -> str:
        raise NotImplementedError

    def verify(self, payload: str, signature: str) -> bool:
        raise NotImplementedError


class HmacSha256TokenSigner(TokenSigner):
    """HMAC-SHA256 over canonical claims. Key from SecretStore only."""

    def __init__(self, key: bytes | None = None, *, secrets: SecretStore | None = None, kid: str = DEFAULT_KID):
        if key is None:
            raw = (secrets or SecretProvider()).get(SIGNING_KEY_ENV)
            if not raw:
                raise RuntimeError("capability_signing_unavailable")
            key = raw.encode("utf-8")
        self._key = key
        self.kid = kid

    def sign(self, payload: str) -> str:
        digest = hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()

    def verify(self, payload: str, signature: str) -> bool:
        expected = self.sign(payload)
        return hmac.compare_digest(expected, str(signature or ""))


@dataclass(frozen=True)
class CapabilityToken:
    token_id: str
    subject_id: str
    capabilities: tuple[str, ...]
    scope: CapabilityScope
    issued_at: datetime
    expires_at: datetime | None
    nonce: str
    version: str = "1"
    workflow_id: str | None = None
    task_id: str | None = None
    kid: str = DEFAULT_KID

    def __post_init__(self):
        object.__setattr__(self, "capabilities", tuple(self.capabilities))

    def canonical_payload(self) -> str:
        body = {
            "token_id": self.token_id,
            "subject_id": self.subject_id,
            "capabilities": sorted(self.capabilities),
            "scope": self.scope.as_dict(),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "nonce": self.nonce,
            "version": self.version,
            "kid": self.kid,
        }
        return json.dumps(body, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class SignedCapabilityToken:
    token: CapabilityToken
    signature: str
    kid: str


def sign_token(token: CapabilityToken, signer: TokenSigner) -> SignedCapabilityToken:
    signature = signer.sign(token.canonical_payload())
    return SignedCapabilityToken(token=token, signature=signature, kid=signer.kid)


def verify_signed_token(signed: SignedCapabilityToken, signer: TokenSigner) -> bool:
    if signed.kid != signer.kid:
        return False
    return signer.verify(signed.token.canonical_payload(), signed.signature)


def token_public_claims(token: CapabilityToken) -> dict:
    return {
        "token_id": token.token_id,
        "subject_id": token.subject_id,
        "kid": token.kid,
        "version": token.version,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
    }


def validate_token(
    signed: SignedCapabilityToken | None,
    *,
    action,
    required: tuple[str, ...],
    signer: TokenSigner | None,
    now: datetime | None = None,
    revoked_ids: frozenset[str] | set[str] = frozenset(),
) -> str | None:
    if signed is None:
        return "token_missing"
    token = signed.token
    if token.token_id in revoked_ids:
        return "token_revoked"
    if signer is not None and not verify_signed_token(signed, signer):
        return "token_invalid"
    stamp = now or utc_now()
    if token.expires_at is not None and token.expires_at <= stamp:
        return "token_expired"
    if token.version != "1":
        return "token_version_invalid"
    if not set(required) <= set(token.capabilities):
        return "capability_missing"
    mismatch = scope_mismatch(token.scope, action)
    if mismatch:
        return mismatch
    if token.workflow_id and token.workflow_id != action.workflow_id:
        return "scope_workflow_mismatch"
    if token.task_id and token.task_id != action.task_id:
        return "scope_task_mismatch"
    return None
