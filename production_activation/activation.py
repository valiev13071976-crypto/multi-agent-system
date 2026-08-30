"""Production traffic activation via canonical RoutingActivationService."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from evals.activation import ActivationError, RoutingActivationService
from evals.promotion import STAGE_PRODUCTION_ELIGIBLE, CandidatePolicy
from production_activation.errors import ACTIVATION_CONFLICT, ACTIVATION_FAILED, ProductionActivationError
from production_activation.models import ActivationAttempt, ActivationState, FinalProductionCandidate, GoLivePlan


class ProductionTrafficActivator:
    """Uses existing RoutingActivationService — no second production router."""

    def __init__(self, *, routing_activation: RoutingActivationService | None = None):
        self.routing_activation = routing_activation or RoutingActivationService()
        self._lock = threading.RLock()
        self._state = ActivationState.GO_LIVE_ELIGIBLE.value
        self._attempts: dict[str, ActivationAttempt] = {}
        self._idempotency: dict[str, ActivationAttempt] = {}

    @property
    def state(self) -> str:
        return self._state

    def restore_state(self, state: str) -> None:
        """Restore process state from durable store after restart (does not activate)."""
        with self._lock:
            if state:
                self._state = state

    def _routing_candidate(self, candidate: FinalProductionCandidate) -> CandidatePolicy:
        policy_ver = candidate.routing_policy_version or "live"
        return CandidatePolicy(
            candidate_id=candidate.candidate_id,
            candidate_version="1",
            base_routing_policy_version=policy_ver,
            proposed_routing_policy_version=policy_ver,
            stage=STAGE_PRODUCTION_ELIGIBLE,
            eval_suite_id="stage5",
            eval_suite_version="1",
            eval_run_id="stage5-activation",
            eval_manifest_hash="stage5",
            model_profile_version=policy_ver,
            production_eligible=True,
            production_active=False,
        )

    def activate(
        self,
        *,
        candidate: FinalProductionCandidate,
        plan: GoLivePlan,
        operator_ref: str,
        expected_policy_version: str,
        idempotency_key: str,
    ) -> ActivationAttempt:
        with self._lock:
            if idempotency_key in self._idempotency:
                return self._idempotency[idempotency_key]
            active_id = self.routing_activation.active_candidate_id
            if self._state == ActivationState.PRODUCTION_ACTIVE.value and active_id not in (None, candidate.candidate_id):
                raise ProductionActivationError(ACTIVATION_CONFLICT, details={"active": active_id})
            attempt = ActivationAttempt(
                attempt_id=f"act-{uuid.uuid4().hex[:12]}",
                candidate_id=candidate.candidate_id,
                plan_id=plan.plan_id,
                authorization_id="",
                operator_ref=operator_ref,
                state=ActivationState.ACTIVATING.value,
            )
            self._state = ActivationState.ACTIVATING.value
            try:
                record = self.routing_activation.activate(
                    self._routing_candidate(candidate),
                    actor_ref=operator_ref,
                    expected_policy_version=expected_policy_version or candidate.routing_policy_version or "live",
                )
                attempt.state = ActivationState.PRODUCTION_ACTIVE.value
                attempt.routing_result = record.as_dict()
                attempt.completed_at = datetime.now(timezone.utc).isoformat()
                self._state = ActivationState.PRODUCTION_ACTIVE.value
            except ActivationError as exc:
                attempt.state = ActivationState.ACTIVATION_FAILED.value
                attempt.error_code = exc.reason_code
                attempt.completed_at = datetime.now(timezone.utc).isoformat()
                self._state = ActivationState.ACTIVATION_FAILED.value
                raise ProductionActivationError(ACTIVATION_FAILED, details={"reason": exc.reason_code}) from exc
            self._attempts[attempt.attempt_id] = attempt
            self._idempotency[idempotency_key] = attempt
            return attempt

    def deactivate(self, *, operator_ref: str, reason: str = "") -> dict:
        with self._lock:
            previous = self.routing_activation.rollback(operator_ref)
            self._state = ActivationState.ROLLED_BACK.value
            return {
                "state": self._state,
                "reason": reason,
                "operator_ref": operator_ref,
                "restored": previous.as_dict() if previous else None,
            }

    def rollback(self, *, operator_ref: str) -> dict:
        return self.deactivate(operator_ref=operator_ref, reason="rollback")

    def get_active_candidate_id(self) -> str | None:
        return self.routing_activation.active_candidate_id
