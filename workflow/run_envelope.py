"""Versioned immutable Run Envelope — Core execution identity contract (P1-ENVELOPE).

Created once at WorkflowEngine.execute and passed as source of truth through
RouterV2 → Pipeline → ExpertManager. Does not replace queue ExecutionContext.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


RUN_ENVELOPE_SCHEMA_VERSION = "1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({RUN_ENVELOPE_SCHEMA_VERSION})


class RunEnvelopeError(ValueError):
    """Fail-closed RunEnvelope validation / version error."""

    def __init__(self, reason_code: str, *, details: dict | None = None):
        self.reason_code = str(reason_code)
        self.details = dict(details or {})
        super().__init__(self.reason_code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_nonempty(name: str, value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        raise RunEnvelopeError(
            "run_envelope_required_field_missing",
            details={"field": name},
        )
    return text


def _parse_dt(value: Any, *, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError as exc:
            raise RunEnvelopeError(
                "run_envelope_invalid_datetime",
                details={"field": field, "value": str(value)},
            ) from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    stamp = value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    else:
        stamp = stamp.astimezone(timezone.utc)
    # Deterministic UTC ISO-8601 with Z suffix.
    return stamp.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RunEnvelope:
    """Immutable versioned execution identity for live Core path."""

    execution_id: str
    request_id: str
    workflow_id: str
    task_id: str
    tenant_id: str
    created_at: datetime
    correlation_id: str
    trace_id: str
    schema_version: str = RUN_ENVELOPE_SCHEMA_VERSION
    user_id: str = ""
    actor_ref: str = ""
    deadline_at: datetime | None = None
    priority: str | None = None
    idempotency_key: str | None = None
    auth_context_version: str | None = None
    capability_scope_ref: str | None = None
    data_scope_ref: str | None = None

    def __post_init__(self):
        version = str(self.schema_version or "").strip()
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise RunEnvelopeError(
                "run_envelope_unknown_schema_version",
                details={"schema_version": version},
            )
        object.__setattr__(self, "schema_version", version)
        object.__setattr__(
            self, "execution_id", _require_nonempty("execution_id", self.execution_id)
        )
        object.__setattr__(
            self, "request_id", _require_nonempty("request_id", self.request_id)
        )
        object.__setattr__(
            self, "workflow_id", _require_nonempty("workflow_id", self.workflow_id)
        )
        object.__setattr__(self, "task_id", _require_nonempty("task_id", self.task_id))
        object.__setattr__(
            self, "tenant_id", _require_nonempty("tenant_id", self.tenant_id)
        )
        object.__setattr__(
            self,
            "correlation_id",
            _require_nonempty("correlation_id", self.correlation_id),
        )
        object.__setattr__(
            self, "trace_id", _require_nonempty("trace_id", self.trace_id)
        )
        created = self.created_at
        if not isinstance(created, datetime):
            raise RunEnvelopeError(
                "run_envelope_required_field_missing",
                details={"field": "created_at"},
            )
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        else:
            created = created.astimezone(timezone.utc)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "user_id", str(self.user_id or ""))
        object.__setattr__(self, "actor_ref", str(self.actor_ref or ""))
        if self.deadline_at is not None:
            object.__setattr__(self, "deadline_at", _parse_dt(self.deadline_at, field="deadline_at"))
        if self.priority is not None:
            object.__setattr__(self, "priority", str(self.priority))
        if self.idempotency_key is not None:
            object.__setattr__(self, "idempotency_key", str(self.idempotency_key))
        if self.auth_context_version is not None:
            object.__setattr__(
                self, "auth_context_version", str(self.auth_context_version)
            )
        if self.capability_scope_ref is not None:
            object.__setattr__(
                self, "capability_scope_ref", str(self.capability_scope_ref)
            )
        if self.data_scope_ref is not None:
            object.__setattr__(self, "data_scope_ref", str(self.data_scope_ref))

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        task_id: str,
        tenant_id: str,
        request_id: str,
        correlation_id: str,
        trace_id: str,
        user_id: str = "",
        actor_ref: str = "",
        execution_id: str | None = None,
        created_at: datetime | None = None,
        deadline_at: datetime | None = None,
        priority: str | None = None,
        idempotency_key: str | None = None,
        auth_context_version: str | None = None,
        capability_scope_ref: str | None = None,
        data_scope_ref: str | None = None,
        schema_version: str = RUN_ENVELOPE_SCHEMA_VERSION,
    ) -> "RunEnvelope":
        return cls(
            schema_version=schema_version,
            execution_id=str(execution_id or uuid.uuid4()),
            request_id=request_id,
            workflow_id=workflow_id,
            task_id=task_id,
            tenant_id=tenant_id,
            created_at=created_at or _utc_now(),
            correlation_id=correlation_id,
            trace_id=trace_id,
            user_id=user_id,
            actor_ref=actor_ref,
            deadline_at=deadline_at,
            priority=priority,
            idempotency_key=idempotency_key,
            auth_context_version=auth_context_version,
            capability_scope_ref=capability_scope_ref,
            data_scope_ref=data_scope_ref,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "request_id": self.request_id,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "created_at": _format_dt(self.created_at),
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "actor_ref": self.actor_ref,
            "deadline_at": _format_dt(self.deadline_at),
            "priority": self.priority,
            "idempotency_key": self.idempotency_key,
            "auth_context_version": self.auth_context_version,
            "capability_scope_ref": self.capability_scope_ref,
            "data_scope_ref": self.data_scope_ref,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "RunEnvelope":
        if not isinstance(payload, Mapping):
            raise RunEnvelopeError(
                "run_envelope_invalid_payload",
                details={"type": type(payload).__name__},
            )
        data = dict(payload)
        version = str(data.get("schema_version") or "").strip()
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise RunEnvelopeError(
                "run_envelope_unknown_schema_version",
                details={"schema_version": version},
            )
        created = _parse_dt(data.get("created_at"), field="created_at")
        if created is None:
            raise RunEnvelopeError(
                "run_envelope_required_field_missing",
                details={"field": "created_at"},
            )
        return cls(
            schema_version=version,
            execution_id=data.get("execution_id"),
            request_id=data.get("request_id"),
            workflow_id=data.get("workflow_id"),
            task_id=data.get("task_id"),
            tenant_id=data.get("tenant_id"),
            created_at=created,
            correlation_id=data.get("correlation_id"),
            trace_id=data.get("trace_id"),
            user_id=str(data.get("user_id") or ""),
            actor_ref=str(data.get("actor_ref") or ""),
            deadline_at=_parse_dt(data.get("deadline_at"), field="deadline_at"),
            priority=data.get("priority"),
            idempotency_key=data.get("idempotency_key"),
            auth_context_version=data.get("auth_context_version"),
            capability_scope_ref=data.get("capability_scope_ref"),
            data_scope_ref=data.get("data_scope_ref"),
        )
