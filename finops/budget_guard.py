"""BudgetGuard — pre-execution authorization, reservations, degrade/terminate."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from finops.budget_ledger import BudgetLedger, build_scope_refs, scope_ref
from finops.budget_models import (
    BUDGET_POLICY_VERSION,
    DECISION_CONTINUE,
    DECISION_DEGRADE,
    DECISION_TERMINATE,
    RES_COMMITTED,
    RES_RECONCILED,
    RES_RESERVED,
    RES_UNCERTAIN,
    SCOPE_AGENT,
    SCOPE_DAILY,
    SCOPE_GLOBAL,
    SCOPE_MODEL,
    SCOPE_MONTHLY,
    SCOPE_PROVIDER,
    SCOPE_TASK,
    BudgetConstraints,
    BudgetDecision,
    BudgetPolicy,
    BudgetReservation,
    utc_now,
)
from finops.budget_policy import load_advanced_budget_policies
from finops.budget_store import (
    BudgetPersistenceUnavailableError,
    BudgetStore,
    InMemoryBudgetStore,
)
from finops.forecast import forecast_from_usage
from finops.models import BudgetLimits, UNKNOWN_COST_DENY
from finops.service import FinOpsService


DEFAULT_RESERVATION_TTL_SECONDS = 300
DEFAULT_ESTIMATE_TOKENS = (1000, 500)


class BudgetGuardError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class BudgetGuard:
    """
    Policy / reservation owner. FinOpsService remains source of actual cost truth.
    """

    policy_version = BUDGET_POLICY_VERSION

    def __init__(
        self,
        *,
        finops: FinOpsService,
        policies: tuple[BudgetPolicy, ...] | None = None,
        store: BudgetStore | None = None,
        ledger: BudgetLedger | None = None,
        required: bool = False,
        reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
        default_estimate_tokens: tuple[int, int] = DEFAULT_ESTIMATE_TOKENS,
        observability=None,
    ):
        self.finops = finops
        self.store = store or InMemoryBudgetStore()
        self.ledger = ledger or BudgetLedger(self.store)
        if policies is None:
            policies = load_advanced_budget_policies(limits=finops._limits)
        self.policies = tuple(p for p in policies if p.enabled)
        self.required = bool(required) or bool(self.policies)
        self.reservation_ttl_seconds = int(reservation_ttl_seconds)
        self.default_estimate_tokens = default_estimate_tokens
        self.observability = observability
        self.last_decision: BudgetDecision | None = None
        self._hard_violation = False

    @property
    def enforcement_active(self) -> bool:
        return self.required and bool(self.policies)

    def _emit(self, event_type: str, **kwargs) -> None:
        obs = self.observability
        if obs is None:
            return
        from observability.helpers import safe_emit

        safe_emit(
            obs,
            event_type,
            context=obs.create_context(task_id=str(kwargs.pop("task_id", "") or "")),
            component="budget_guard",
            provider=str(kwargs.pop("provider", "") or ""),
            model=str(kwargs.pop("model", "") or ""),
            status=str(kwargs.pop("status", "") or ""),
            error_code=kwargs.pop("error_code", None),
            metadata=kwargs.pop("metadata", None),
        )

    def _inc(self, counter: str, **labels) -> None:
        obs = self.observability
        if obs is None or getattr(obs, "metrics", None) is None:
            return
        try:
            obs.metrics.inc(counter, labels=labels or None)
        except Exception:
            return

    def estimate_request_cost(
        self, provider: str, model: str, *, input_tokens=None, output_tokens=None
    ) -> Decimal | None:
        if input_tokens is None or output_tokens is None:
            input_tokens, output_tokens = self.default_estimate_tokens
        return self.finops.estimate(provider, model, input_tokens, output_tokens)

    def get_remaining_budget(self, scope: str, key: str = "") -> Decimal | None:
        hard = None
        for policy in self.policies:
            if policy.scope == scope and (not policy.scope_key or policy.scope_key == key):
                if policy.hard_limit is not None:
                    hard = policy.hard_limit if hard is None else min(hard, policy.hard_limit)
        return self.ledger.get_remaining(hard_limit=hard, scope=scope, key=key)

    def forecast(self, *, scope: str = SCOPE_GLOBAL, key: str = ""):
        remaining = self.get_remaining_budget(scope, key)
        hard = None
        for p in self.policies:
            if p.scope == scope and p.hard_limit is not None:
                hard = p.hard_limit
        records = self.finops._store.records()
        result = forecast_from_usage(
            records, remaining_budget=remaining, window_limit=hard
        )
        self._emit(
            "budget.forecasted",
            status="ok",
            metadata={
                "remaining_calls": result.estimated_remaining_calls,
                "sample_size": result.sample_size,
            },
        )
        return result

    def _policy_key(self, policy: BudgetPolicy, *, task_id, agent_id, provider, model, when):
        if policy.scope_key:
            return policy.scope_key
        if policy.scope == SCOPE_TASK:
            return task_id
        if policy.scope == SCOPE_AGENT:
            return agent_id or ""
        if policy.scope == SCOPE_PROVIDER:
            return provider
        if policy.scope == SCOPE_MODEL:
            return f"{provider}/{model}"
        if policy.scope == SCOPE_DAILY:
            from finops.service import _day_bounds

            start, _ = _day_bounds(when)
            return start.date().isoformat()
        if policy.scope == SCOPE_MONTHLY:
            from finops.service import _month_bounds

            start, _ = _month_bounds(when)
            return f"{start.year:04d}-{start.month:02d}"
        return ""

    def evaluate(
        self,
        *,
        task_id: str,
        provider: str,
        model: str,
        estimated_cost: Decimal | None,
        agent_id: str | None = None,
        capable_candidates: tuple[tuple[str, str], ...] = (),
        dry_run: bool = False,
        now=None,
    ) -> BudgetDecision:
        stamp = now or utc_now()
        self.ledger.expire_stale(now=stamp)

        if self._hard_violation:
            decision = BudgetDecision(
                decision=DECISION_TERMINATE,
                reason_code="budget_hard_limit_exceeded",
                scope=SCOPE_GLOBAL,
                requested_cost=estimated_cost,
                reserved_cost=None,
                remaining_budget=Decimal("0"),
            )
            self.last_decision = decision
            return decision

        # Unknown cost: reuse FinOps unknown-cost policy (never treat as zero).
        if estimated_cost is None:
            legacy = self.finops.check_budget(None, when=stamp, task_id=task_id)
            if not legacy.allowed:
                decision = BudgetDecision(
                    decision=DECISION_TERMINATE,
                    reason_code="unknown_cost_denied",
                    scope=SCOPE_GLOBAL,
                    requested_cost=None,
                    reserved_cost=None,
                    remaining_budget=None,
                    metadata_safe={"legacy_reason": legacy.reason},
                )
            else:
                decision = BudgetDecision(
                    decision=DECISION_CONTINUE,
                    reason_code="unknown_cost_allowed",
                    scope=SCOPE_GLOBAL,
                    requested_cost=None,
                    reserved_cost=None,
                    remaining_budget=None,
                )
            self.last_decision = decision
            self._emit(
                "budget.evaluated",
                task_id=task_id,
                provider=provider,
                model=model,
                status=decision.decision.lower(),
                error_code=decision.reason_code,
            )
            return decision

        if not self.policies:
            decision = BudgetDecision(
                decision=DECISION_CONTINUE,
                reason_code="no_budget_policies",
                scope=SCOPE_GLOBAL,
                requested_cost=estimated_cost,
                reserved_cost=None,
                remaining_budget=None,
            )
            self.last_decision = decision
            return decision

        # Also honor legacy FinOps hard check (preserves existing denies).
        legacy = self.finops.check_budget(
            estimated_cost, when=stamp, task_id=task_id
        )
        if not legacy.allowed:
            decision = BudgetDecision(
                decision=DECISION_TERMINATE,
                reason_code=legacy.reason,
                scope=SCOPE_TASK if legacy.reason == "per_task_limit" else SCOPE_DAILY,
                requested_cost=estimated_cost,
                reserved_cost=None,
                remaining_budget=None,
            )
            self.last_decision = decision
            self._emit(
                "budget.evaluated",
                task_id=task_id,
                provider=provider,
                model=model,
                status="terminate",
                error_code=decision.reason_code,
            )
            self._inc("budget_terminate_total", decision="TERMINATE", provider=provider)
            return decision

        worst = DECISION_CONTINUE
        reasons: list[str] = []
        blocking_scope = SCOPE_GLOBAL
        remaining_min: Decimal | None = None
        hard_hit = None
        soft_hit = None
        max_affordable: Decimal | None = None

        for policy in self.policies:
            key = self._policy_key(
                policy,
                task_id=task_id,
                agent_id=agent_id,
                provider=provider,
                model=model,
                when=stamp,
            )
            reserved, _, spent = self.store.get_totals(scope_ref(policy.scope, key))
            used = reserved + spent
            if policy.hard_limit is not None:
                remaining = policy.hard_limit - used
                remaining_min = (
                    remaining if remaining_min is None else min(remaining_min, remaining)
                )
                max_affordable = (
                    remaining if max_affordable is None else min(max_affordable, remaining)
                )
                if estimated_cost > remaining:
                    worst = DECISION_TERMINATE
                    blocking_scope = policy.scope
                    hard_hit = policy.hard_limit
                    reasons.append(f"{policy.scope}:budget_hard_limit_exceeded")
                    continue
            threshold = policy.degrade_threshold
            if threshold is None:
                threshold = policy.soft_limit
            if threshold is not None and policy.hard_limit is not None:
                remaining = policy.hard_limit - used
                if remaining <= threshold:
                    if worst != DECISION_TERMINATE:
                        worst = DECISION_DEGRADE
                        soft_hit = threshold
                        blocking_scope = policy.scope
                        reasons.append(f"{policy.scope}:budget_soft_threshold")
            elif threshold is not None and estimated_cost >= threshold:
                if worst != DECISION_TERMINATE:
                    worst = DECISION_DEGRADE
                    soft_hit = threshold
                    blocking_scope = policy.scope
                    reasons.append(f"{policy.scope}:budget_soft_threshold")

        recommended_provider = None
        recommended_model = None
        excluded_providers: list[str] = []
        excluded_models: list[str] = []

        if worst == DECISION_DEGRADE:
            # Prefer cheaper capable candidates under max_affordable.
            affordable = []
            for cand_provider, cand_model in capable_candidates:
                cost = self.estimate_request_cost(cand_provider, cand_model)
                if cost is None:
                    continue
                if max_affordable is not None and cost > max_affordable:
                    excluded_providers.append(cand_provider)
                    excluded_models.append(f"{cand_provider}/{cand_model}")
                    continue
                affordable.append((cost, cand_provider, cand_model))
            affordable.sort(key=lambda row: (row[0], row[1], row[2]))
            if affordable:
                _, recommended_provider, recommended_model = affordable[0]
            else:
                # No cheaper capable route fits → TERMINATE
                worst = DECISION_TERMINATE
                reasons.append("budget_no_affordable_capable_route")

        if worst == DECISION_TERMINATE:
            decision = BudgetDecision(
                decision=DECISION_TERMINATE,
                reason_code=reasons[-1].split(":")[-1]
                if reasons
                else "budget_hard_limit_exceeded",
                scope=blocking_scope,
                requested_cost=estimated_cost,
                reserved_cost=None,
                remaining_budget=remaining_min,
                hard_limit=hard_hit,
                soft_limit=soft_hit,
                max_affordable_cost=max_affordable,
                excluded_providers=tuple(excluded_providers),
                excluded_models=tuple(excluded_models),
                scope_reasons=tuple(reasons),
            )
            self.last_decision = decision
            self._emit(
                "budget.evaluated",
                task_id=task_id,
                provider=provider,
                model=model,
                status="terminate",
                error_code=decision.reason_code,
                metadata={"scope_reasons": list(reasons)},
            )
            self._inc(
                "budget_terminate_total",
                decision="TERMINATE",
                provider=provider,
                model_family=model.split("-")[0] if model else "",
            )
            if not dry_run:
                self._emit("budget.terminated", task_id=task_id, status="terminated")
            return decision

        if worst == DECISION_DEGRADE:
            decision = BudgetDecision(
                decision=DECISION_DEGRADE,
                reason_code="budget_soft_threshold",
                scope=blocking_scope,
                requested_cost=estimated_cost,
                reserved_cost=None,
                remaining_budget=remaining_min,
                hard_limit=hard_hit,
                soft_limit=soft_hit,
                recommended_provider=recommended_provider,
                recommended_model=recommended_model,
                max_affordable_cost=max_affordable,
                excluded_providers=tuple(dict.fromkeys(excluded_providers)),
                excluded_models=tuple(dict.fromkeys(excluded_models)),
                scope_reasons=tuple(reasons),
            )
            self.last_decision = decision
            self._emit(
                "budget.evaluated",
                task_id=task_id,
                provider=provider,
                model=model,
                status="degrade",
                error_code=decision.reason_code,
            )
            self._inc("budget_degrade_total", decision="DEGRADE", provider=provider)
            if not dry_run:
                self._emit(
                    "budget.degraded",
                    task_id=task_id,
                    provider=recommended_provider or provider,
                    model=recommended_model or model,
                    status="degraded",
                )
            return decision

        decision = BudgetDecision(
            decision=DECISION_CONTINUE,
            reason_code="within_budget",
            scope=SCOPE_GLOBAL,
            requested_cost=estimated_cost,
            reserved_cost=None,
            remaining_budget=remaining_min,
            max_affordable_cost=max_affordable,
            scope_reasons=tuple(reasons),
        )
        self.last_decision = decision
        self._emit(
            "budget.evaluated",
            task_id=task_id,
            provider=provider,
            model=model,
            status="continue",
            error_code=decision.reason_code,
        )
        return decision

    def constraints_for_router(self, decision: BudgetDecision) -> BudgetConstraints:
        preferred = ()
        if decision.recommended_provider and decision.recommended_model:
            preferred = ((decision.recommended_provider, decision.recommended_model),)
        return BudgetConstraints(
            max_affordable_cost=decision.max_affordable_cost,
            excluded_providers=decision.excluded_providers,
            excluded_models=decision.excluded_models,
            preferred_cheaper=preferred,
            decision=decision.decision,
            reason_code=decision.reason_code,
        )

    def reserve(
        self,
        *,
        task_id: str,
        provider: str,
        model: str,
        estimated_cost: Decimal,
        agent_id: str | None = None,
        now=None,
        dry_run: bool = False,
    ) -> BudgetReservation:
        if dry_run:
            raise BudgetGuardError("dry_run_cannot_reserve")
        stamp = now or utc_now()
        decision = self.evaluate(
            task_id=task_id,
            provider=provider,
            model=model,
            estimated_cost=estimated_cost,
            agent_id=agent_id,
            now=stamp,
        )
        if decision.decision == DECISION_TERMINATE:
            self._inc("budget_reservation_denied_total", decision="TERMINATE", provider=provider)
            raise BudgetGuardError(decision.reason_code)
        # DEGRADE still allows reserve on recommended route only if caller passes that provider.
        try:
            with self.store.begin_reserve_transaction():
                # Re-check under lock for concurrency
                decision2 = self.evaluate(
                    task_id=task_id,
                    provider=provider,
                    model=model,
                    estimated_cost=estimated_cost,
                    agent_id=agent_id,
                    now=stamp,
                )
                if decision2.decision == DECISION_TERMINATE:
                    self._inc(
                        "budget_reservation_denied_total",
                        decision="TERMINATE",
                        provider=provider,
                    )
                    raise BudgetGuardError(decision2.reason_code)
                refs = build_scope_refs(
                    task_id=task_id,
                    agent_id=agent_id,
                    provider=provider,
                    model=model,
                    when=stamp,
                )
                reservation = BudgetReservation(
                    reservation_id=str(uuid.uuid4()),
                    scope_refs=refs,
                    task_id=task_id,
                    agent_id=agent_id,
                    provider=provider,
                    model=model,
                    estimated_cost=estimated_cost,
                    currency="USD",
                    status=RES_RESERVED,
                    created_at=stamp,
                    expires_at=stamp + timedelta(seconds=self.reservation_ttl_seconds),
                    metadata_safe={"policy_version": BUDGET_POLICY_VERSION},
                )
                self.store.insert_reservation(reservation)
                for ref in refs:
                    self.store.add_reserved(ref, estimated_cost)
        except BudgetPersistenceUnavailableError as exc:
            self._inc("budget_reservation_denied_total", decision="TERMINATE", provider=provider)
            raise BudgetGuardError("budget_persistence_unavailable") from exc

        self._emit(
            "budget.reserved",
            task_id=task_id,
            provider=provider,
            model=model,
            status="reserved",
            metadata={"reservation_id": reservation.reservation_id, "cost": str(estimated_cost)},
        )
        self._inc("budget_reservations_total", decision="CONTINUE", provider=provider)
        if self.observability and getattr(self.observability, "metrics", None):
            try:
                self.observability.metrics.inc(
                    "budget_reserved_amount",
                    amount=int(estimated_cost * 100),
                    labels={"provider": provider},
                )
            except Exception:
                pass
        return reservation

    def reconcile(
        self,
        reservation_id: str,
        *,
        actual_cost: Decimal | None,
        usage_record_key: str | None = None,
        uncertain: bool = False,
        now=None,
    ) -> BudgetReservation:
        stamp = now or utc_now()
        current = self.store.get_reservation(reservation_id)
        if current is None:
            raise BudgetGuardError("reservation_not_found")
        if current.status in {RES_RECONCILED, RES_COMMITTED} and current.actual_cost is not None:
            return current

        if uncertain or actual_cost is None:
            updated = BudgetReservation(
                reservation_id=current.reservation_id,
                scope_refs=current.scope_refs,
                task_id=current.task_id,
                provider=current.provider,
                model=current.model,
                estimated_cost=current.estimated_cost,
                currency=current.currency,
                status=RES_UNCERTAIN,
                created_at=current.created_at,
                expires_at=current.expires_at,
                agent_id=current.agent_id,
                committed_at=stamp,
                released_at=None,
                actual_cost=None,
                usage_record_key=usage_record_key,
                metadata_safe={**dict(current.metadata_safe), "uncertain": True},
                version=current.version,
            )
            result = self.store.update_reservation(updated, expected_version=current.version)
            self._emit(
                "budget.reconciled",
                task_id=current.task_id,
                provider=current.provider,
                model=current.model,
                status="uncertain",
            )
            return result

        actual = Decimal(str(actual_cost))
        estimated = current.estimated_cost
        # Move from reserved → spent for actual; release unused reserved.
        for ref in current.scope_refs:
            self.store.release_reserved(ref, estimated)
            self.store.add_spent(ref, actual)

        if actual > estimated:
            # overage already fully spent; check future hard blocks
            for policy in self.policies:
                if policy.hard_limit is None:
                    continue
                key = self._policy_key(
                    policy,
                    task_id=current.task_id,
                    agent_id=current.agent_id,
                    provider=current.provider,
                    model=current.model,
                    when=stamp,
                )
                remaining = self.ledger.get_remaining(
                    hard_limit=policy.hard_limit, scope=policy.scope, key=key
                )
                if remaining is not None and remaining < 0:
                    self._hard_violation = True

        updated = BudgetReservation(
            reservation_id=current.reservation_id,
            scope_refs=current.scope_refs,
            task_id=current.task_id,
            provider=current.provider,
            model=current.model,
            estimated_cost=current.estimated_cost,
            currency=current.currency,
            status=RES_RECONCILED,
            created_at=current.created_at,
            expires_at=current.expires_at,
            agent_id=current.agent_id,
            committed_at=stamp,
            released_at=stamp if actual < estimated else None,
            actual_cost=actual,
            usage_record_key=usage_record_key,
            metadata_safe=dict(current.metadata_safe),
            version=current.version,
        )
        result = self.store.update_reservation(updated, expected_version=current.version)
        self._emit(
            "budget.reconciled",
            task_id=current.task_id,
            provider=current.provider,
            model=current.model,
            status="reconciled",
            metadata={"actual": str(actual), "estimated": str(estimated)},
        )
        if self.observability and getattr(self.observability, "metrics", None):
            try:
                self.observability.metrics.inc(
                    "budget_spent_amount",
                    amount=int(actual * 100),
                    labels={"provider": current.provider},
                )
                if actual < estimated:
                    self.observability.metrics.inc(
                        "budget_released_amount",
                        amount=int((estimated - actual) * 100),
                        labels={"provider": current.provider},
                    )
            except Exception:
                pass
        return result

    def release(self, reservation_id: str, *, now=None) -> BudgetReservation | None:
        result = self.ledger.release(reservation_id, now=now)
        if result is not None:
            self._emit(
                "budget.released",
                task_id=result.task_id,
                provider=result.provider,
                model=result.model,
                status=result.status,
            )
        return result

    def require_reservation_or_raise(self) -> None:
        if self.enforcement_active:
            raise BudgetGuardError("budget_reservation_required")
