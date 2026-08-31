"""Activation authorization — explicit, bound, auditable, durable-consumption ready."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from production_activation.commands import activation_confirmation_token
from production_activation.errors import AUTHORIZATION_DENIED, AUTHORIZATION_REPLAY, AUTHORIZATION_STALE, ProductionActivationError
from production_activation.models import ActivationAuthorization, FinalProductionCandidate, GoLivePlan


def _parse_expiry(expires_at: str) -> datetime:
    expires = datetime.fromisoformat(expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires


class ActivationAuthorizer:
    def __init__(self, *, ttl_seconds: int = 900):
        self.ttl_seconds = ttl_seconds
        self._issued: dict[str, ActivationAuthorization] = {}
        self._consumed_keys: set[str] = set()

    def issue(
        self,
        *,
        candidate: FinalProductionCandidate,
        plan: GoLivePlan,
        operator_ref: str,
        idempotency_key: str,
        secret: str = "",
        release_identity: str = "",
    ) -> ActivationAuthorization:
        now = datetime.now(timezone.utc)
        token = activation_confirmation_token(
            actor_ref=operator_ref,
            candidate_fingerprint=candidate.fingerprint,
            deployment_fingerprint=candidate.deployment_id,
            plan_fingerprint=plan.fingerprint,
            secret=secret,
        )
        auth = ActivationAuthorization(
            authorization_id=f"auth-{uuid.uuid4().hex[:12]}",
            candidate_fingerprint=candidate.fingerprint,
            deployment_fingerprint=candidate.deployment_id,
            plan_fingerprint=plan.fingerprint,
            operator_ref=operator_ref,
            confirmation_token=token,
            idempotency_key=idempotency_key,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            candidate_id=candidate.candidate_id,
            plan_id=plan.plan_id,
            release_identity=release_identity,
        )
        self._issued[auth.authorization_id] = auth
        return auth

    def verify(
        self,
        *,
        authorization: ActivationAuthorization,
        candidate: FinalProductionCandidate,
        plan: GoLivePlan,
        operator_ref: str,
        confirmation_token: str,
        idempotency_key: str = "",
    ) -> ActivationAuthorization:
        if authorization.consumed:
            raise ProductionActivationError(AUTHORIZATION_REPLAY)
        if idempotency_key and idempotency_key in self._consumed_keys:
            raise ProductionActivationError(AUTHORIZATION_REPLAY, details={"idempotency_key": idempotency_key})
        if datetime.now(timezone.utc) > _parse_expiry(authorization.expires_at):
            raise ProductionActivationError(AUTHORIZATION_STALE)
        if authorization.operator_ref != operator_ref:
            raise ProductionActivationError(AUTHORIZATION_DENIED, details={"operator": "mismatch"})
        if authorization.candidate_id and authorization.candidate_id != candidate.candidate_id:
            raise ProductionActivationError(AUTHORIZATION_DENIED, details={"candidate_id": "mismatch"})
        if authorization.plan_id and authorization.plan_id != plan.plan_id:
            raise ProductionActivationError(AUTHORIZATION_DENIED, details={"plan_id": "mismatch"})
        if authorization.candidate_fingerprint != candidate.fingerprint:
            raise ProductionActivationError(AUTHORIZATION_DENIED, details={"candidate": "mismatch"})
        if authorization.deployment_fingerprint != candidate.deployment_id:
            raise ProductionActivationError(AUTHORIZATION_DENIED, details={"deployment": "mismatch"})
        if authorization.plan_fingerprint != plan.fingerprint:
            raise ProductionActivationError(AUTHORIZATION_DENIED, details={"plan": "mismatch"})
        expected = activation_confirmation_token(
            actor_ref=operator_ref,
            candidate_fingerprint=candidate.fingerprint,
            deployment_fingerprint=candidate.deployment_id,
            plan_fingerprint=plan.fingerprint,
        )
        if confirmation_token != expected and confirmation_token != authorization.confirmation_token:
            raise ProductionActivationError(AUTHORIZATION_DENIED, details={"confirmation": "invalid"})
        return authorization

    def consume(self, authorization: ActivationAuthorization, *, idempotency_key: str, attempt_id: str = "") -> ActivationAuthorization:
        """Process-local consume (tests / same-process). Durable authority is SqliteProductionActivationStore.consume_authorization."""
        if authorization.authorization_id in self._issued:
            auth = self._issued[authorization.authorization_id]
            object.__setattr__(auth, "consumed", True)
            if attempt_id:
                object.__setattr__(auth, "consumed_at", datetime.now(timezone.utc).isoformat())
                object.__setattr__(auth, "attempt_id", attempt_id)
        self._consumed_keys.add(idempotency_key)
        return self.mark_consumed(authorization, attempt_id=attempt_id)

    @staticmethod
    def mark_consumed(authorization: ActivationAuthorization, *, attempt_id: str = "") -> ActivationAuthorization:
        return ActivationAuthorization(
            authorization_id=authorization.authorization_id,
            candidate_fingerprint=authorization.candidate_fingerprint,
            deployment_fingerprint=authorization.deployment_fingerprint,
            plan_fingerprint=authorization.plan_fingerprint,
            operator_ref=authorization.operator_ref,
            confirmation_token=authorization.confirmation_token,
            idempotency_key=authorization.idempotency_key,
            issued_at=authorization.issued_at,
            expires_at=authorization.expires_at,
            consumed=True,
            consumed_at=datetime.now(timezone.utc).isoformat(),
            attempt_id=attempt_id,
            candidate_id=authorization.candidate_id,
            plan_id=authorization.plan_id,
            release_identity=authorization.release_identity,
        )
