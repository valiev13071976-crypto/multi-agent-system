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
    DECISION_SKIP_MODEL,
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
    SCOPE_TENANT,
    BudgetConstraints,
    BudgetDecision,
    BudgetPolicy,
    BudgetReservation,
    merge_budget_decision,
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


REASON_BUDGET_TENANT_REQUIRED = "budget_tenant_required"
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

    def _resolve_obs_context(
        self,
        *,
        parent_context=None,
        envelope=None,
        task_id: str = "",
        workflow_id: str = "",
        tenant_id: str = "",
        actor_ref: str = "",
        correlation_id: str | None = None,
    ):
        """Prefer parent/envelope lineage; never invent a competing root when parent exists."""
        obs = self.observability
        if obs is None:
            return None

        if parent_context is not None:
            return obs.child_span(
                parent_context,
                workflow_id=workflow_id or None,
                task_id=task_id or None,
                tenant_id=tenant_id or None,
                actor_ref=actor_ref or None,
            )

        if envelope is not None:
            env_workflow = str(getattr(envelope, "workflow_id", "") or "")
            env_task = str(getattr(envelope, "task_id", "") or task_id or "")
            env_tenant = str(getattr(envelope, "tenant_id", "") or "")
            env_actor = str(getattr(envelope, "actor_ref", "") or "")
            existing = (
                obs.context_for_workflow(env_workflow) if env_workflow else None
            )
            if existing is not None:
                return existing.child(
                    task_id=env_task or existing.task_id,
                    actor_ref=env_actor or None,
                    tenant_id=env_tenant or None,
                )
            # No bound parent: reuse envelope corr/trace (do not mint new root IDs).
            from observability.context import ObservabilityContext

            return ObservabilityContext(
                correlation_id=str(envelope.correlation_id),
                trace_id=str(envelope.trace_id),
                span_id=str(uuid.uuid4()),
                parent_span_id=None,
                workflow_id=env_workflow,
                task_id=env_task,
                actor_ref=env_actor,
                tenant_id=env_tenant,
            )

        resolved_workflow = str(workflow_id or "")
        if resolved_workflow:
            existing = obs.context_for_workflow(resolved_workflow)
            if existing is not None:
                return existing.child(
                    task_id=task_id or existing.task_id,
                    actor_ref=actor_ref or None,
                    tenant_id=tenant_id or None,
                )

        # Legacy callers without parent/envelope: keep prior root behavior.
        return obs.create_context(
            correlation_id=correlation_id,
            workflow_id=resolved_workflow,
            task_id=str(task_id or ""),
            actor_ref=str(actor_ref or ""),
            tenant_id=str(tenant_id or ""),
        )

    def _emit(self, event_type: str, **kwargs) -> None:
        obs = self.observability
        if obs is None:
            return
        from observability.helpers import safe_emit

        parent_context = kwargs.pop("parent_context", None)
        envelope = kwargs.pop("envelope", None)
        task_id = str(kwargs.pop("task_id", "") or "")
        workflow_id = str(kwargs.pop("workflow_id", "") or "")
        tenant_id = str(kwargs.pop("tenant_id", "") or "")
        actor_ref = str(kwargs.pop("actor_ref", "") or "")
        correlation_id = kwargs.pop("correlation_id", None)
        context = self._resolve_obs_context(
            parent_context=parent_context,
            envelope=envelope,
            task_id=task_id,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            actor_ref=actor_ref,
            correlation_id=correlation_id,
        )
        safe_emit(
            obs,
            event_type,
            context=context,
            component="budget_guard",
            provider=str(kwargs.pop("provider", "") or ""),
            model=str(kwargs.pop("model", "") or ""),
            status=str(kwargs.pop("status", "") or ""),
            error_code=kwargs.pop("error_code", None),
            metadata=kwargs.pop("metadata", None),
        )

    def _lineage_emit_kw(
        self,
        *,
        envelope=None,
        parent_context=None,
        task_id: str = "",
        tenant_id: str | None = None,
        workflow_id: str | None = None,
        actor_ref: str | None = None,
    ) -> dict:
        """Request-local obs identity for budget.* emits (never stored on self)."""
        if envelope is not None:
            return {
                "envelope": envelope,
                "parent_context": parent_context,
                "task_id": str(getattr(envelope, "task_id", None) or task_id or ""),
                "workflow_id": str(getattr(envelope, "workflow_id", "") or ""),
                "tenant_id": str(getattr(envelope, "tenant_id", "") or ""),
                "actor_ref": str(getattr(envelope, "actor_ref", "") or ""),
            }
        return {
            "envelope": None,
            "parent_context": parent_context,
            "task_id": str(task_id or ""),
            "workflow_id": str(workflow_id or ""),
            "tenant_id": str(tenant_id or ""),
            "actor_ref": str(actor_ref or ""),
        }

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

    def _normalize_tenant_id(self, tenant_id) -> str | None:
        tid = str(tenant_id or "").strip()
        return tid or None

    def _tenant_scoped_policies_active(self) -> bool:
        return any(p.scope == SCOPE_TENANT for p in self.policies)

    def _tenant_id_from_reservation(self, reservation: BudgetReservation) -> str | None:
        meta = dict(reservation.metadata_safe or {})
        tid = self._normalize_tenant_id(meta.get("tenant_id"))
        if tid:
            return tid
        for ref in reservation.scope_refs:
            if str(ref).startswith(f"{SCOPE_TENANT}:") and len(str(ref)) > len(SCOPE_TENANT) + 1:
                return str(ref).split(":", 1)[1]
        return None

    def _missing_tenant_decision(self, estimated_cost: Decimal | None) -> BudgetDecision:
        return BudgetDecision(
            decision=DECISION_TERMINATE,
            reason_code=REASON_BUDGET_TENANT_REQUIRED,
            scope=SCOPE_TENANT,
            requested_cost=estimated_cost,
            reserved_cost=None,
            remaining_budget=None,
        )

    def _policy_key(
        self,
        policy: BudgetPolicy,
        *,
        task_id,
        agent_id,
        provider,
        model,
        when,
        tenant_id=None,
    ):
        # Tenant scope always keys by request tenant — never collapse to scope_key alone.
        if policy.scope == SCOPE_TENANT:
            return str(tenant_id or "").strip()
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
        tenant_id: str | None = None,
        capable_candidates: tuple[tuple[str, str], ...] = (),
        dry_run: bool = False,
        now=None,
        envelope=None,
        parent_context=None,
        workflow_id: str | None = None,
        actor_ref: str | None = None,
    ) -> BudgetDecision:
        stamp = now or utc_now()
        self.ledger.expire_stale(now=stamp)
        lineage = self._lineage_emit_kw(
            envelope=envelope,
            parent_context=parent_context,
            task_id=task_id,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            actor_ref=actor_ref,
        )

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

        if self._tenant_scoped_policies_active() and self._normalize_tenant_id(tenant_id) is None:
            decision = self._missing_tenant_decision(estimated_cost)
            self.last_decision = decision
            self._emit(
                "budget.evaluated",
                provider=provider,
                model=model,
                status="terminate",
                error_code=decision.reason_code,
                **lineage,
            )
            self._inc(
                "budget_terminate_total",
                decision="TERMINATE",
                provider=provider,
            )
            return decision

        # Unknown cost: reuse FinOps unknown-cost policy (never treat as zero).
        if estimated_cost is None:
            legacy = self.finops.check_budget(None, when=stamp, task_id=task_id)
            if not legacy.allowed:
                # Provider-specific: this candidate cannot run; peers may still proceed.
                decision = BudgetDecision(
                    decision=DECISION_SKIP_MODEL,
                    reason_code="unknown_cost_denied",
                    scope=SCOPE_GLOBAL,
                    requested_cost=None,
                    reserved_cost=None,
                    remaining_budget=None,
                    excluded_providers=(provider,),
                    excluded_models=(f"{provider}/{model}",),
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
                provider=provider,
                model=model,
                status=decision.decision.lower(),
                error_code=decision.reason_code,
                **lineage,
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

        # Legacy FinOps deny for this estimate — provider-specific SKIP_MODEL.
        legacy = self.finops.check_budget(
            estimated_cost, when=stamp, task_id=task_id
        )
        if not legacy.allowed:
            decision = BudgetDecision(
                decision=DECISION_SKIP_MODEL,
                reason_code=legacy.reason,
                scope=SCOPE_TASK if legacy.reason == "per_task_limit" else SCOPE_DAILY,
                requested_cost=estimated_cost,
                reserved_cost=None,
                remaining_budget=None,
                excluded_providers=(provider,),
                excluded_models=(f"{provider}/{model}",),
            )
            self.last_decision = decision
            self._emit(
                "budget.evaluated",
                provider=provider,
                model=model,
                status="skip_model",
                error_code=decision.reason_code,
                **lineage,
            )
            self._inc("budget_skip_model_total", decision="SKIP_MODEL", provider=provider)
            return decision

        worst = DECISION_CONTINUE
        reasons: list[str] = []
        blocking_scope = SCOPE_GLOBAL
        remaining_min: Decimal | None = None
        hard_hit = None
        soft_hit = None
        max_affordable: Decimal | None = None
        resolved_tenant = self._normalize_tenant_id(tenant_id)

        for policy in self.policies:
            key = self._policy_key(
                policy,
                task_id=task_id,
                agent_id=agent_id,
                provider=provider,
                model=model,
                when=stamp,
                tenant_id=resolved_tenant,
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
                    worst = merge_budget_decision(worst, DECISION_SKIP_MODEL)
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
                    worst = merge_budget_decision(worst, DECISION_DEGRADE)
                    soft_hit = threshold
                    blocking_scope = policy.scope
                    reasons.append(f"{policy.scope}:budget_soft_threshold")
            elif threshold is not None and estimated_cost >= threshold:
                worst = merge_budget_decision(worst, DECISION_DEGRADE)
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
                # Monotonic: exclude non-recommended (more expensive) candidates.
                for cand_provider, cand_model in capable_candidates:
                    if cand_provider != recommended_provider:
                        excluded_providers.append(cand_provider)
                        excluded_models.append(f"{cand_provider}/{cand_model}")
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
                excluded_providers=tuple(dict.fromkeys(excluded_providers)),
                excluded_models=tuple(dict.fromkeys(excluded_models)),
                scope_reasons=tuple(reasons),
            )
            self.last_decision = decision
            self._emit(
                "budget.evaluated",
                provider=provider,
                model=model,
                status="terminate",
                error_code=decision.reason_code,
                metadata={"scope_reasons": list(reasons)},
                **lineage,
            )
            self._inc(
                "budget_terminate_total",
                decision="TERMINATE",
                provider=provider,
                model_family=model.split("-")[0] if model else "",
            )
            if not dry_run:
                self._emit("budget.terminated", status="terminated", **lineage)
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
                provider=provider,
                model=model,
                status="degrade",
                error_code=decision.reason_code,
                **lineage,
            )
            self._inc("budget_degrade_total", decision="DEGRADE", provider=provider)
            if not dry_run:
                self._emit(
                    "budget.degraded",
                    provider=recommended_provider or provider,
                    model=recommended_model or model,
                    status="degraded",
                    **lineage,
                )
            return decision

        if worst == DECISION_SKIP_MODEL:
            decision = BudgetDecision(
                decision=DECISION_SKIP_MODEL,
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
                excluded_providers=(provider,),
                excluded_models=(f"{provider}/{model}",),
                scope_reasons=tuple(reasons),
            )
            self.last_decision = decision
            self._emit(
                "budget.evaluated",
                provider=provider,
                model=model,
                status="skip_model",
                error_code=decision.reason_code,
                metadata={"scope_reasons": list(reasons)},
                **lineage,
            )
            self._inc("budget_skip_model_total", decision="SKIP_MODEL", provider=provider)
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
            provider=provider,
            model=model,
            status="continue",
            error_code=decision.reason_code,
            **lineage,
        )
        return decision

    def constraints_for_router(self, decision: BudgetDecision) -> BudgetConstraints:
        preferred = ()
        if decision.recommended_provider and decision.recommended_model:
            preferred = ((decision.recommended_provider, decision.recommended_model),)
        skipped = tuple(decision.excluded_providers)
        return BudgetConstraints(
            max_affordable_cost=decision.max_affordable_cost,
            remaining_budget=decision.remaining_budget,
            excluded_providers=decision.excluded_providers,
            excluded_models=decision.excluded_models,
            skipped_providers=skipped,
            preferred_cheaper=preferred,
            unknown_cost_policy=self.finops._limits.unknown_cost_policy,
            decision=decision.decision,
            reason_code=decision.reason_code,
            metadata_safe={
                **dict(decision.metadata_safe),
                "budget_decision": decision.decision,
            },
        )

    def _routing_remaining_for_policy(
        self,
        policy: BudgetPolicy,
        *,
        task_id: str,
        agent_id: str | None,
        provider: str,
        model: str,
        when,
        tenant_id: str | None = None,
    ) -> Decimal | None:
        if policy.hard_limit is None:
            return None
        key = self._policy_key(
            policy,
            task_id=task_id,
            agent_id=agent_id,
            provider=provider,
            model=model,
            when=when,
            tenant_id=tenant_id,
        )
        reserved, _, spent = self.store.get_totals(scope_ref(policy.scope, key))
        return policy.hard_limit - (reserved + spent)

    def _max_affordable_for_routing(
        self,
        *,
        task_id: str,
        agent_id: str | None,
        when,
        tenant_id: str | None = None,
    ) -> Decimal | None:
        """Min remaining hard budget across scopes that do not need a provider key."""

        max_affordable: Decimal | None = None
        for policy in self.policies:
            if policy.hard_limit is None:
                continue
            if policy.scope in {SCOPE_PROVIDER, SCOPE_MODEL, SCOPE_AGENT}:
                # Resolved per-candidate in _routing_candidate_allowed.
                continue
            remaining = self._routing_remaining_for_policy(
                policy,
                task_id=task_id,
                agent_id=agent_id,
                provider="",
                model="",
                when=when,
                tenant_id=tenant_id,
            )
            if remaining is None:
                continue
            max_affordable = (
                remaining if max_affordable is None else min(max_affordable, remaining)
            )
        return max_affordable

    def _routing_candidate_allowed(
        self,
        *,
        task_id: str,
        provider: str,
        model: str,
        estimated_cost: Decimal | None,
        agent_id: str | None,
        when,
        tenant_id: str | None = None,
    ) -> bool:
        """Hard-budget eligibility only. Unknown cost is never treated as zero."""

        legacy = self.finops.check_budget(
            estimated_cost, when=when, task_id=task_id
        )
        if not legacy.allowed:
            return False
        if estimated_cost is None:
            # Allowed only when FinOps unknown-cost policy allows (already checked).
            return True
        for policy in self.policies:
            if policy.hard_limit is None:
                continue
            remaining = self._routing_remaining_for_policy(
                policy,
                task_id=task_id,
                agent_id=agent_id,
                provider=provider,
                model=model,
                when=when,
                tenant_id=tenant_id,
            )
            if remaining is not None and estimated_cost > remaining:
                return False
        return True

    def routing_constraints(
        self,
        *,
        task_id: str,
        candidates: tuple[tuple[str, str], ...] = (),
        agent_id: str | None = None,
        tenant_id: str | None = None,
        now=None,
    ) -> BudgetConstraints | None:
        """Read-only pre-routing budget view. Never reserves or spends.

        Hard-ineligible candidates are SKIP_MODEL exclusions. Soft-threshold
        DEGRADE preference is applied here so ModelRouter can restrict to the
        cheapest hard-eligible candidates (monotonic — no silent re-upgrade).
        """

        if not self.enforcement_active:
            return None

        stamp = now or utc_now()
        resolved_tenant = self._normalize_tenant_id(tenant_id)

        if self._tenant_scoped_policies_active() and resolved_tenant is None:
            excluded = tuple(dict.fromkeys(p for p, _m in candidates))
            excluded_models = tuple(f"{p}/{m}" for p, m in candidates)
            costs = {
                cand_provider: self.estimate_request_cost(cand_provider, cand_model)
                for cand_provider, cand_model in candidates
            }
            return BudgetConstraints(
                max_affordable_cost=Decimal("0"),
                remaining_budget=Decimal("0"),
                excluded_providers=excluded,
                excluded_models=excluded_models,
                skipped_providers=excluded,
                candidate_costs=costs,
                unknown_cost_policy=self.finops._limits.unknown_cost_policy,
                decision=DECISION_TERMINATE,
                reason_code=REASON_BUDGET_TENANT_REQUIRED,
                metadata_safe={"read_only": True, "reserved": False},
            )

        max_affordable = self._max_affordable_for_routing(
            task_id=task_id,
            agent_id=agent_id,
            when=stamp,
            tenant_id=resolved_tenant,
        )
        excluded: list[str] = []
        excluded_models: list[str] = []
        costs: dict[str, Decimal | None] = {}
        overall = DECISION_CONTINUE
        soft_hit = False

        # Soft-threshold detection (scope remaining, independent of candidate estimate).
        for policy in self.policies:
            if policy.hard_limit is None:
                continue
            if policy.scope in {SCOPE_PROVIDER, SCOPE_MODEL, SCOPE_AGENT}:
                continue
            threshold = policy.degrade_threshold
            if threshold is None:
                threshold = policy.soft_limit
            if threshold is None:
                continue
            remaining = self._routing_remaining_for_policy(
                policy,
                task_id=task_id,
                agent_id=agent_id,
                provider="",
                model="",
                when=stamp,
                tenant_id=resolved_tenant,
            )
            if remaining is not None and remaining <= threshold:
                soft_hit = True
                overall = merge_budget_decision(overall, DECISION_DEGRADE)

        for cand_provider, cand_model in candidates:
            cost = self.estimate_request_cost(cand_provider, cand_model)
            costs[cand_provider] = cost
            if not self._routing_candidate_allowed(
                task_id=task_id,
                provider=cand_provider,
                model=cand_model,
                estimated_cost=cost,
                agent_id=agent_id,
                when=stamp,
                tenant_id=resolved_tenant,
            ):
                excluded.append(cand_provider)
                excluded_models.append(f"{cand_provider}/{cand_model}")
                overall = merge_budget_decision(overall, DECISION_SKIP_MODEL)

        preferred: list[tuple[str, str]] = []
        reason = "routing_budget_filter"
        if soft_hit:
            affordable = []
            for cand_provider, cand_model in candidates:
                if cand_provider in excluded:
                    continue
                cost = costs.get(cand_provider)
                if cost is None:
                    continue
                if max_affordable is not None and cost > max_affordable:
                    excluded.append(cand_provider)
                    excluded_models.append(f"{cand_provider}/{cand_model}")
                    overall = merge_budget_decision(overall, DECISION_SKIP_MODEL)
                    continue
                affordable.append((cost, cand_provider, cand_model))
            affordable.sort(key=lambda row: (row[0], row[1], row[2]))
            if affordable:
                overall = merge_budget_decision(overall, DECISION_DEGRADE)
                reason = "budget_soft_threshold"
                # Monotonic: only the cheapest hard-eligible candidate is preferred.
                _, p_id, p_model = affordable[0]
                preferred = [(p_id, p_model)]
                for cost, cand_provider, cand_model in affordable[1:]:
                    excluded.append(cand_provider)
                    excluded_models.append(f"{cand_provider}/{cand_model}")
            else:
                # Soft pressure but no affordable eligible candidate.
                overall = DECISION_TERMINATE
                reason = "budget_no_affordable_capable_route"
        elif excluded and len(excluded) < len(candidates):
            overall = merge_budget_decision(overall, DECISION_SKIP_MODEL)
            reason = "budget_skip_model"
        elif excluded and candidates and len(set(excluded)) >= len({p for p, _ in candidates}):
            overall = DECISION_TERMINATE
            reason = "budget_no_affordable_capable_route"

        excluded_u = tuple(dict.fromkeys(excluded))
        return BudgetConstraints(
            max_affordable_cost=max_affordable,
            remaining_budget=max_affordable,
            excluded_providers=excluded_u,
            excluded_models=tuple(dict.fromkeys(excluded_models)),
            skipped_providers=excluded_u,
            preferred_cheaper=tuple(preferred),
            candidate_costs=costs,
            unknown_cost_policy=self.finops._limits.unknown_cost_policy,
            decision=overall,
            reason_code=reason,
            metadata_safe={
                "read_only": True,
                "reserved": False,
                "tenant_id": resolved_tenant or "",
                "budget_decision": overall,
                "soft_degrade": soft_hit,
            },
        )

    def reserve(
        self,
        *,
        task_id: str,
        provider: str,
        model: str,
        estimated_cost: Decimal,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        now=None,
        dry_run: bool = False,
        capable_candidates: tuple[tuple[str, str], ...] = (),
        envelope=None,
        parent_context=None,
        workflow_id: str | None = None,
        actor_ref: str | None = None,
    ) -> BudgetReservation:
        if dry_run:
            raise BudgetGuardError("dry_run_cannot_reserve")
        stamp = now or utc_now()
        resolved_tenant = self._normalize_tenant_id(tenant_id)
        lineage = self._lineage_emit_kw(
            envelope=envelope,
            parent_context=parent_context,
            task_id=task_id,
            tenant_id=resolved_tenant,
            workflow_id=workflow_id,
            actor_ref=actor_ref,
        )
        decision = self.evaluate(
            task_id=task_id,
            provider=provider,
            model=model,
            estimated_cost=estimated_cost,
            agent_id=agent_id,
            tenant_id=resolved_tenant,
            now=stamp,
            capable_candidates=capable_candidates,
            envelope=envelope,
            parent_context=parent_context,
            workflow_id=workflow_id,
            actor_ref=actor_ref,
        )
        if decision.decision == DECISION_TERMINATE:
            self._inc("budget_reservation_denied_total", decision="TERMINATE", provider=provider)
            raise BudgetGuardError(decision.reason_code)
        if decision.decision == DECISION_SKIP_MODEL:
            self._inc("budget_reservation_denied_total", decision="SKIP_MODEL", provider=provider)
            raise BudgetGuardError(decision.reason_code)
        # DEGRADE still allows reserve on recommended route only if caller passes that provider.
        if (
            decision.decision == DECISION_DEGRADE
            and decision.recommended_provider
            and provider != decision.recommended_provider
        ):
            self._inc("budget_reservation_denied_total", decision="DEGRADE", provider=provider)
            raise BudgetGuardError("budget_degrade_not_recommended")
        try:
            with self.store.begin_reserve_transaction():
                # Re-check under lock for concurrency
                decision2 = self.evaluate(
                    task_id=task_id,
                    provider=provider,
                    model=model,
                    estimated_cost=estimated_cost,
                    agent_id=agent_id,
                    tenant_id=resolved_tenant,
                    now=stamp,
                    capable_candidates=capable_candidates,
                    envelope=envelope,
                    parent_context=parent_context,
                    workflow_id=workflow_id,
                    actor_ref=actor_ref,
                )
                if decision2.decision == DECISION_TERMINATE:
                    self._inc(
                        "budget_reservation_denied_total",
                        decision="TERMINATE",
                        provider=provider,
                    )
                    raise BudgetGuardError(decision2.reason_code)
                if decision2.decision == DECISION_SKIP_MODEL:
                    self._inc(
                        "budget_reservation_denied_total",
                        decision="SKIP_MODEL",
                        provider=provider,
                    )
                    raise BudgetGuardError(decision2.reason_code)
                if (
                    decision2.decision == DECISION_DEGRADE
                    and decision2.recommended_provider
                    and provider != decision2.recommended_provider
                ):
                    raise BudgetGuardError("budget_degrade_not_recommended")
                refs = build_scope_refs(
                    task_id=task_id,
                    agent_id=agent_id,
                    provider=provider,
                    model=model,
                    when=stamp,
                    tenant_id=resolved_tenant,
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
                    metadata_safe={
                        "policy_version": BUDGET_POLICY_VERSION,
                        "tenant_id": resolved_tenant or "",
                    },
                )
                self.store.insert_reservation(reservation)
                for ref in refs:
                    self.store.add_reserved(ref, estimated_cost)
        except BudgetPersistenceUnavailableError as exc:
            self._inc("budget_reservation_denied_total", decision="TERMINATE", provider=provider)
            raise BudgetGuardError("budget_persistence_unavailable") from exc

        self._emit(
            "budget.reserved",
            provider=provider,
            model=model,
            status="reserved",
            metadata={
                "reservation_id": reservation.reservation_id,
                "cost": str(estimated_cost),
                "tenant_id": resolved_tenant or "",
            },
            **lineage,
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
        envelope=None,
        parent_context=None,
        workflow_id: str | None = None,
        actor_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> BudgetReservation:
        stamp = now or utc_now()
        current = self.store.get_reservation(reservation_id)
        if current is None:
            raise BudgetGuardError("reservation_not_found")
        if current.status in {RES_RECONCILED, RES_COMMITTED} and current.actual_cost is not None:
            return current

        lineage = self._lineage_emit_kw(
            envelope=envelope,
            parent_context=parent_context,
            task_id=current.task_id,
            tenant_id=tenant_id or self._tenant_id_from_reservation(current),
            workflow_id=workflow_id,
            actor_ref=actor_ref,
        )

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
                provider=current.provider,
                model=current.model,
                status="uncertain",
                **lineage,
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
            res_tenant = self._tenant_id_from_reservation(current)
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
                    tenant_id=res_tenant,
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
            provider=current.provider,
            model=current.model,
            status="reconciled",
            metadata={"actual": str(actual), "estimated": str(estimated)},
            **lineage,
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

    def release(
        self,
        reservation_id: str,
        *,
        now=None,
        envelope=None,
        parent_context=None,
        workflow_id: str | None = None,
        actor_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> BudgetReservation | None:
        result = self.ledger.release(reservation_id, now=now)
        if result is not None:
            lineage = self._lineage_emit_kw(
                envelope=envelope,
                parent_context=parent_context,
                task_id=result.task_id,
                tenant_id=tenant_id or self._tenant_id_from_reservation(result),
                workflow_id=workflow_id,
                actor_ref=actor_ref,
            )
            self._emit(
                "budget.released",
                provider=result.provider,
                model=result.model,
                status=result.status,
                **lineage,
            )
        return result

    def require_reservation_or_raise(self) -> None:
        if self.enforcement_active:
            raise BudgetGuardError("budget_reservation_required")
