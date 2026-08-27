"""Security audit trail — no secrets or raw payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from security.redaction import redact

_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "api_key",
        "authorization",
        "bearer",
    }
)


def _sanitize_metadata(metadata: Mapping | None) -> dict:
    cleaned: dict = {}
    for key, value in dict(metadata or {}).items():
        if str(key).lower() in _FORBIDDEN_METADATA_KEYS:
            continue
        if isinstance(value, str):
            cleaned[str(key)] = redact(value)
        elif isinstance(value, Mapping):
            cleaned[str(key)] = _sanitize_metadata(value)
        else:
            cleaned[str(key)] = value
    return cleaned


@dataclass(frozen=True)
class SecurityAuditRecord:
    event_type: str
    actor_ref: str
    tenant_ref: str
    resource_ref: str
    timestamp: datetime
    outcome: str
    reason_code: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self, "metadata", MappingProxyType(_sanitize_metadata(self.metadata))
        )


class SecurityAuditLog:
    """Append-only in-process audit log."""

    def __init__(self, *, max_records: int = 5000):
        self._records: list[SecurityAuditRecord] = []
        self._max = max_records

    def record(
        self,
        event_type: str,
        *,
        actor_ref: str = "",
        tenant_ref: str = "",
        resource_ref: str = "",
        outcome: str = "ok",
        reason_code: str | None = None,
        metadata=None,
    ) -> SecurityAuditRecord:
        rec = SecurityAuditRecord(
            event_type=redact(str(event_type)),
            actor_ref=redact(str(actor_ref or "")),
            tenant_ref=redact(str(tenant_ref or "")),
            resource_ref=redact(str(resource_ref or "")),
            timestamp=datetime.now(timezone.utc),
            outcome=redact(str(outcome)),
            reason_code=redact(str(reason_code)) if reason_code else None,
            metadata=metadata or {},
        )
        self._records.append(rec)
        if len(self._records) > self._max:
            self._records = self._records[-self._max :]
        return rec

    def events(self) -> tuple[SecurityAuditRecord, ...]:
        return tuple(self._records)

    def for_tenant(self, tenant_ref: str) -> tuple[SecurityAuditRecord, ...]:
        return tuple(r for r in self._records if r.tenant_ref == tenant_ref)
