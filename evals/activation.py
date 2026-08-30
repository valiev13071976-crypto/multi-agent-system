"""Deliberate routing-policy production activation (separate from offline promotion).

``PromotionGovernor`` / ``ReleaseGate`` never call this service. Activation is an
explicit operator step that records PRODUCTION_ACTIVE without mutating offline
governance candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from evals.models import utc_now
from evals.promotion import (
    STAGE_PRODUCTION_ACTIVE,
    STAGE_PRODUCTION_ELIGIBLE,
    CandidatePolicy,
    PromotionGovernor,
)
from evals.versions import ROUTING_POLICY_VERSION

# Safety: offline governor must never grow an activate_production API.
assert not hasattr(PromotionGovernor, "activate_production")

# Optional staleness guard (None / 0 disables).
DEFAULT_MAX_CANDIDATE_AGE = timedelta(days=30)


class ActivationError(ValueError):
    def __init__(self, reason_code: str, *, details: dict | None = None):
        self.reason_code = str(reason_code)
        self.details = dict(details or {})
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class ActivationRecord:
    candidate_id: str
    policy_version: str
    actor_ref: str
    activated_at: datetime
    stage: str = STAGE_PRODUCTION_ACTIVE
    candidate_version: str = ""
    model_profile_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "policy_version": self.policy_version,
            "actor_ref": self.actor_ref,
            "activated_at": self.activated_at.isoformat(),
            "stage": self.stage,
            "candidate_version": self.candidate_version,
            "model_profile_version": self.model_profile_version,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ActivationEvent:
    event_type: str
    actor_ref: str
    timestamp: datetime
    candidate_id: str = ""
    policy_version: str = ""
    reason_code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "actor_ref": self.actor_ref,
            "timestamp": self.timestamp.isoformat(),
            "candidate_id": self.candidate_id,
            "policy_version": self.policy_version,
            "reason_code": self.reason_code,
            "details": dict(self.details),
        }


class RoutingActivationService:
    """Holds the visible active routing policy version + auditable history."""

    def __init__(self, *, max_candidate_age: timedelta | None = DEFAULT_MAX_CANDIDATE_AGE):
        self.active_policy_version: str | None = None
        self.active_candidate_id: str | None = None
        self.history: list[ActivationRecord] = []
        self._events: list[ActivationEvent] = []
        self._previous: ActivationRecord | None = None
        self._active: ActivationRecord | None = None
        self.max_candidate_age = max_candidate_age

    def _emit(self, event: ActivationEvent) -> None:
        self._events.append(event)

    def events(self) -> tuple[ActivationEvent, ...]:
        return tuple(self._events)

    def get_active(self) -> ActivationRecord | None:
        return self._active

    def activate(
        self,
        candidate: CandidatePolicy,
        *,
        actor_ref: str,
        expected_policy_version: str,
        now: datetime | None = None,
    ) -> ActivationRecord:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        actor = str(actor_ref or "").strip()
        if not actor:
            raise ActivationError("actor_ref_required")

        if candidate.stage != STAGE_PRODUCTION_ELIGIBLE:
            raise ActivationError(
                "candidate_not_production_eligible_stage",
                details={"stage": candidate.stage},
            )
        if not candidate.production_eligible:
            raise ActivationError("candidate_not_production_eligible")
        if candidate.production_active:
            raise ActivationError("candidate_already_production_active")

        expected = str(expected_policy_version or "").strip()
        proposed = str(candidate.proposed_routing_policy_version or "")
        profile_ver = str(candidate.model_profile_version or "")
        live = str(ROUTING_POLICY_VERSION)
        if expected != live and expected not in {proposed, profile_ver}:
            raise ActivationError(
                "expected_policy_version_mismatch",
                details={
                    "expected": expected,
                    "routing_policy_version": live,
                    "proposed": proposed,
                    "model_profile_version": profile_ver,
                },
            )
        if expected not in {proposed, profile_ver, live}:
            raise ActivationError(
                "candidate_policy_version_mismatch",
                details={
                    "expected": expected,
                    "proposed": proposed,
                    "model_profile_version": profile_ver,
                },
            )
        if expected != proposed and expected != profile_ver:
            # expected may equal live pin; still require candidate aligns
            if proposed != live and profile_ver != live and proposed != expected:
                raise ActivationError(
                    "candidate_policy_version_mismatch",
                    details={
                        "expected": expected,
                        "proposed": proposed,
                        "model_profile_version": profile_ver,
                    },
                )

        if self.max_candidate_age is not None and candidate.updated_at is not None:
            updated = candidate.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if stamp - updated > self.max_candidate_age:
                raise ActivationError(
                    "candidate_stale",
                    details={"updated_at": updated.isoformat()},
                )

        policy_version = proposed or expected or live
        record = ActivationRecord(
            candidate_id=candidate.candidate_id,
            policy_version=policy_version,
            actor_ref=actor,
            activated_at=stamp,
            stage=STAGE_PRODUCTION_ACTIVE,
            candidate_version=str(candidate.candidate_version or ""),
            model_profile_version=profile_ver,
        )
        if self._active is not None:
            self._previous = self._active
            self.history.append(self._active)
        self._active = record
        self.active_policy_version = record.policy_version
        self.active_candidate_id = record.candidate_id
        self._emit(
            ActivationEvent(
                event_type="routing.activated",
                actor_ref=actor,
                timestamp=stamp,
                candidate_id=record.candidate_id,
                policy_version=record.policy_version,
            )
        )
        return record

    def rollback(self, actor_ref: str, *, now: datetime | None = None) -> ActivationRecord | None:
        stamp = now or utc_now()
        actor = str(actor_ref or "").strip() or "system"
        previous = self._previous
        cleared = self._active
        if cleared is not None:
            self.history.append(cleared)
        self._active = previous
        self._previous = None
        if previous is not None:
            self.active_policy_version = previous.policy_version
            self.active_candidate_id = previous.candidate_id
        else:
            self.active_policy_version = None
            self.active_candidate_id = None
        self._emit(
            ActivationEvent(
                event_type="routing.rollback",
                actor_ref=actor,
                timestamp=stamp,
                candidate_id=str(getattr(cleared, "candidate_id", "") or ""),
                policy_version=str(getattr(cleared, "policy_version", "") or ""),
                reason_code="rollback",
                details={
                    "restored_candidate_id": str(
                        getattr(previous, "candidate_id", "") or ""
                    ),
                },
            )
        )
        return previous
