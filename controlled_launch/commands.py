"""Governed launch commands."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class LockCandidateCommand:
    candidate_id: str
    actor_ref: str
    confirmation_token: str = ""


@dataclass(frozen=True)
class StartInternalCommand:
    candidate_id: str
    actor_ref: str
    confirmation_token: str = ""


@dataclass(frozen=True)
class StartShadowCommand:
    candidate_id: str
    actor_ref: str
    confirmation_token: str = ""


@dataclass(frozen=True)
class StartCanaryCommand:
    candidate_id: str
    plan_id: str
    actor_ref: str
    confirmation_token: str = ""


@dataclass(frozen=True)
class HoldRolloutCommand:
    candidate_id: str
    actor_ref: str
    reason: str = ""


@dataclass(frozen=True)
class AbortRolloutCommand:
    candidate_id: str
    actor_ref: str
    reason: str = ""


@dataclass(frozen=True)
class RollbackRolloutCommand:
    candidate_id: str
    actor_ref: str
    confirmation_token: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AdvanceRolloutCommand:
    candidate_id: str
    actor_ref: str
    target_step: str
    confirmation_token: str = ""


def launch_confirmation_token(*, actor_ref: str, action: str, target_id: str, secret: str = "") -> str:
    raw = f"{actor_ref}:{action}:{target_id}:{secret}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]
