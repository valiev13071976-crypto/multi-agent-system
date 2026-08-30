"""Governed admin command contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class CancelRunCommand:
    workflow_id: str
    tenant_id: str
    reason: str = ""
    expected_status: str | None = None


@dataclass(frozen=True)
class RedriveDLQCommand:
    task_id: str
    tenant_id: str
    idempotency_key: str
    reason: str = ""


@dataclass(frozen=True)
class DrainWorkerPoolCommand:
    pool_name: str
    reason: str = ""
    confirmation_token: str = ""


@dataclass(frozen=True)
class ActivateRoutingCommand:
    candidate_id: str
    expected_policy_version: str
    confirmation_token: str
    reason: str = ""


@dataclass(frozen=True)
class RollbackRoutingCommand:
    confirmation_token: str
    reason: str = ""


@dataclass(frozen=True)
class ApprovalDecisionCommand:
    workflow_id: str
    approval_id: str
    tenant_id: str
    decision: str
    idempotency_key: str
    reason: str = ""


def confirmation_token(*, actor_ref: str, action: str, target_id: str, secret: str = "") -> str:
    raw = f"{actor_ref}:{action}:{target_id}:{secret}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]
