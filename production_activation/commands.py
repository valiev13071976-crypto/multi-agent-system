"""Governed production activation commands."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class PrepareActivationCommand:
    candidate_id: str
    production_url: str
    operator_ref: str
    monitoring_destination: str
    alert_destination: str


@dataclass(frozen=True)
class AuthorizeActivationCommand:
    candidate_id: str
    plan_id: str
    operator_ref: str
    idempotency_key: str


@dataclass(frozen=True)
class ActivateProductionCommand:
    candidate_id: str
    plan_id: str
    authorization_id: str
    operator_ref: str
    confirmation_token: str
    idempotency_key: str
    expected_policy_version: str = ""


@dataclass(frozen=True)
class RollbackProductionCommand:
    candidate_id: str
    operator_ref: str
    reason: str = ""
    confirmation_token: str = ""


def activation_confirmation_token(
    *,
    actor_ref: str,
    candidate_fingerprint: str,
    deployment_fingerprint: str,
    plan_fingerprint: str,
    secret: str = "",
) -> str:
    raw = f"{actor_ref}:{candidate_fingerprint}:{deployment_fingerprint}:{plan_fingerprint}:{secret}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]
