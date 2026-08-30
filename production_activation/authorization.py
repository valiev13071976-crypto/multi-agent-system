"""Activation authorization — explicit, bound, auditable."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from production_activation.commands import activation_confirmation_token
from production_activation.errors import AUTHORIZATION_DENIED, AUTHORIZATION_REPLAY, AUTHORIZATION_STALE, ProductionActivationError
from production_activation.models import ActivationAuthorization, FinalProductionCandidate, GoLivePlan


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
        idempotency_key: str,
    ) -> ActivationAuthorization:
        if authorization.consumed:
            raise ProductionActivationError(AUTHORIZATION_REPLAY)
        if idempotency_key in self._consumed_keys:
            raise ProductionActivationError(AUTHORIZATION_REPLAY, details={"idempotency_key": idempotency_key})
        expires = datetime.fromisoformat(authorization.expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            raise ProductionActivationError(AUTHORIZATION_STALE)
        if authorization.operator_ref != operator_ref:
            raise ProductionActivationError(AUTHORIZATION_DENIED, details={"operator": "mismatch"})
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

    def consume(self, authorization: ActivationAuthorization, *, idempotency_key: str) -> None:
        if authorization.authorization_id in self._issued:
            auth = self._issued[authorization.authorization_id]
            object.__setattr__(auth, "consumed", True)
        self._consumed_keys.add(idempotency_key)
