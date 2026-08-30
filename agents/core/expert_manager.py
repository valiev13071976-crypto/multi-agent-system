import asyncio
import time
import uuid
from datetime import datetime, timezone

from agents.provider_result import ProviderResult, provider_result_from_text
from finops.models import CURRENCY_USD, UsageRecord
from security.redaction import redact


PROVIDER_IDS = (
    "openai",
    "anthropic",
    "gemini",
    "grok",
    "deepseek",
    "moonshot",
    "mistral",
)


class FinOpsBudgetDeniedError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ExpertManager:
    """
    Запускает выбранных providers одновременно.
    """

    def __init__(
        self,
        openai=None,
        anthropic=None,
        gemini=None,
        grok=None,
        deepseek=None,
        moonshot=None,
        mistral=None,
        finops=None,
        budget_guard=None,
    ):
        self.openai = openai
        self.anthropic = anthropic
        self.gemini = gemini
        self.grok = grok
        self.deepseek = deepseek
        self.moonshot = moonshot
        self.mistral = mistral
        self.finops = finops
        self.budget_guard = budget_guard
        self.health_tracker = None
        self.runtime_stats = None
        self.provider_governor = None
        self.observability = None
        self.execution_lane = "interactive"
        self.last_errors = {}
        self.last_provider_results = {}
        self.last_usage = []
        self.last_task_id = None
        self.last_workflow_id = None
        self.last_request_id = None
        self.last_tenant_id = None
        self.last_user_id = None
        self.last_actor_ref = None
        self.last_budget_exceeded = False
        self.last_budget_decision = None
        self.last_guard_decision = None
        self.last_reservations = {}
        self.last_run_envelope = None
        self.provider_calls = 0

    def get_provider(self, provider_id: str):
        if provider_id not in PROVIDER_IDS:
            return None
        return getattr(self, provider_id)

    def _attribution_obs_context(
        self,
        *,
        workflow_id: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        tenant_id: str | None = None,
        actor_ref: str | None = None,
    ):
        obs = self.observability
        if obs is None:
            return None
        resolved_workflow = str(
            workflow_id if workflow_id is not None else self.last_workflow_id or ""
        )
        resolved_task = str(task_id if task_id is not None else self.last_task_id or "")
        resolved_request = (
            request_id if request_id is not None else self.last_request_id
        )
        resolved_actor = str(
            actor_ref if actor_ref is not None else self.last_actor_ref or ""
        )
        resolved_tenant = str(
            tenant_id if tenant_id is not None else self.last_tenant_id or ""
        )
        if resolved_workflow:
            existing = obs.context_for_workflow(resolved_workflow)
            if existing is not None:
                return existing.child(task_id=resolved_task or existing.task_id)
        return obs.create_context(
            correlation_id=resolved_request or None,
            workflow_id=resolved_workflow,
            task_id=resolved_task,
            actor_ref=resolved_actor,
            tenant_id=resolved_tenant,
        )

    def _emit_provider_outcome(
        self,
        *,
        provider_id: str,
        model_id: str,
        success: bool,
        error_code: str | None = None,
        exception_type: str | None = None,
        duration_ms: int | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        tenant_id: str | None = None,
        actor_ref: str | None = None,
    ) -> None:
        obs = self.observability
        if obs is not None:
            from observability.helpers import safe_emit

            safe_emit(
                obs,
                "provider.completed" if success else "provider.failed",
                context=self._attribution_obs_context(
                    workflow_id=workflow_id,
                    task_id=task_id,
                    request_id=request_id,
                    tenant_id=tenant_id,
                    actor_ref=actor_ref,
                ),
                component="provider",
                provider=provider_id,
                model=model_id,
                status="ok" if success else "failed",
                error_code=error_code,
                exception_type=exception_type,
                duration_ms=duration_ms,
            )

    def _record_runtime_outcome(
        self,
        *,
        provider_id: str,
        model_id: str,
        result,
        latency_ms: float | None,
        cost=None,
    ) -> None:
        aggregator = self.runtime_stats
        if aggregator is None:
            return
        if isinstance(result, BaseException):
            from agents.routing_health import is_qualifying_provider_failure

            if is_qualifying_provider_failure(result):
                aggregator.record_failure(
                    provider_id,
                    model_id,
                    latency_ms=latency_ms,
                )
            return
        aggregator.record_success(
            provider_id,
            model_id,
            latency_ms=latency_ms,
            cost=cost,
        )

    def _record_health_outcome(
        self,
        *,
        provider_id: str,
        model_id: str,
        result,
        latency_ms: float | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        tenant_id: str | None = None,
        actor_ref: str | None = None,
    ) -> None:
        tracker = self.health_tracker
        emit_kw = dict(
            workflow_id=workflow_id,
            task_id=task_id,
            request_id=request_id,
            tenant_id=tenant_id,
            actor_ref=actor_ref,
        )
        if isinstance(result, BaseException):
            from agents.routing_health import is_qualifying_provider_failure

            if is_qualifying_provider_failure(result):
                if tracker is not None:
                    tracker.record_failure(
                        provider_id,
                        model_id,
                        error_class=type(result).__name__,
                    )
                self._emit_provider_outcome(
                    provider_id=provider_id,
                    model_id=model_id,
                    success=False,
                    error_code="provider_failure",
                    exception_type=type(result).__name__,
                    duration_ms=(
                        int(latency_ms) if latency_ms is not None else None
                    ),
                    **emit_kw,
                )
            return
        if tracker is not None:
            tracker.record_success(provider_id, model_id)
        self._emit_provider_outcome(
            provider_id=provider_id,
            model_id=model_id,
            success=True,
            duration_ms=int(latency_ms) if latency_ms is not None else None,
            **emit_kw,
        )

    def _normalize_result(self, provider_id, agent, result) -> ProviderResult:
        if isinstance(result, ProviderResult):
            return result
        model_id = getattr(agent, "model", "") or ""
        if isinstance(result, str):
            return provider_result_from_text(provider_id, model_id, result)
        return provider_result_from_text(provider_id, model_id, str(result))

    def _record_usage(
        self,
        result: ProviderResult,
        *,
        task_id: str | None = None,
        workflow_id: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
        actor_ref: str | None = None,
        execution_id: str | None = None,
        usage_out: list | None = None,
    ) -> UsageRecord | None:
        if self.finops is None:
            return None
        # Prefer explicit request-local identity; fall back to diagnostic last_* only
        # for direct callers / backward-compatible unit tests.
        # actor_ref / execution_id are never taken from last_* (P1-USAGE ownership).
        resolved_task = task_id if task_id is not None else self.last_task_id
        resolved_workflow = (
            workflow_id if workflow_id is not None else self.last_workflow_id
        )
        resolved_tenant = tenant_id if tenant_id is not None else self.last_tenant_id
        resolved_user = user_id if user_id is not None else self.last_user_id
        resolved_request = (
            request_id if request_id is not None else self.last_request_id
        )
        resolved_actor = str(actor_ref or "")
        resolved_execution = str(execution_id or "")
        estimated_cost = self.finops.estimate(
            result.provider_id,
            result.model_id,
            result.input_tokens,
            result.output_tokens,
        )
        quote = self.finops.quote(result.provider_id, result.model_id)
        record = UsageRecord(
            task_id=resolved_task,
            provider_id=result.provider_id,
            model_id=result.model_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            estimated_cost=estimated_cost,
            currency=quote.currency if quote else CURRENCY_USD,
            timestamp=datetime.now(timezone.utc),
            workflow_id=str(resolved_workflow or ""),
            tenant_id=str(resolved_tenant or ""),
            user_id=str(resolved_user or ""),
            request_id=str(resolved_request or ""),
            actor_ref=resolved_actor,
            execution_id=resolved_execution,
        )
        self.finops.record_usage(record)
        if usage_out is not None:
            usage_out.append(record)
        else:
            self.last_usage.append(record)
            self.last_budget_exceeded = self.finops.is_over_limit(
                when=record.timestamp,
                task_id=resolved_task,
            )
        return record

    async def _run_provider_timed(
        self,
        provider_id,
        agent,
        prompt: str,
        *,
        envelope=None,
        parent_context=None,
        workflow_id=None,
        task_id=None,
        tenant_id=None,
        actor_ref=None,
        request_id=None,
    ):
        started = time.perf_counter()
        model_id = getattr(agent, "model", "") or ""
        slot_id = None
        gov = self.provider_governor
        # Request-local governor obs lineage (never stored on shared governor).
        if envelope is not None:
            gov_lineage = {"envelope": envelope}
        else:
            gov_lineage = {}
            if parent_context is not None:
                gov_lineage["parent_context"] = parent_context
            if workflow_id:
                gov_lineage["workflow_id"] = workflow_id
            if task_id:
                gov_lineage["task_id"] = task_id
            if tenant_id:
                gov_lineage["tenant_id"] = tenant_id
            if actor_ref:
                gov_lineage["actor_ref"] = actor_ref
            if request_id:
                gov_lineage["correlation_id"] = request_id
        if gov is not None:
            try:
                from providers.governor import ProviderCapacityUnavailable

                admit = getattr(gov, "admit", None)
                if envelope is not None and callable(admit):
                    slot_id = admit(
                        provider_id=provider_id,
                        model_id=model_id,
                        lane=getattr(self, "execution_lane", None) or "interactive",
                        worker_id="expert-manager",
                        **gov_lineage,
                    )
                else:
                    slot_id = gov.acquire(
                        provider_id=provider_id,
                        model_id=model_id,
                        lane=getattr(self, "execution_lane", None) or "interactive",
                        worker_id="expert-manager",
                        **gov_lineage,
                    )
            except ProviderCapacityUnavailable as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                return provider_id, exc, elapsed_ms
            except Exception:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                from providers.governor import ProviderCapacityUnavailable

                return (
                    provider_id,
                    ProviderCapacityUnavailable("provider_governor_unavailable"),
                    elapsed_ms,
                )
        try:
            result = await agent.run(prompt)
        except BaseException as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if gov is not None and slot_id is not None:
                try:
                    gov.release(slot_id, **gov_lineage)
                except Exception:
                    pass
                # 429 / Retry-After
                from providers.errors import is_rate_limit_error, parse_retry_after_from_exc

                if is_rate_limit_error(exc):
                    retry_after = parse_retry_after_from_exc(exc)
                    try:
                        gov.record_429(
                            provider_id,
                            model_id,
                            retry_after_seconds=retry_after,
                            **gov_lineage,
                        )
                    except Exception:
                        pass
                else:
                    try:
                        gov.record_failure(
                            provider_id,
                            model_id,
                            error_code=type(exc).__name__,
                            **gov_lineage,
                        )
                    except Exception:
                        pass
            return provider_id, exc, elapsed_ms
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if gov is not None and slot_id is not None:
            try:
                gov.release(slot_id, **gov_lineage)
                gov.record_success(provider_id, model_id, **gov_lineage)
            except Exception:
                pass
            tokens = getattr(result, "total_tokens", None)
            if tokens is not None:
                try:
                    gov.record_tokens(
                        provider_id, model_id, tokens=tokens, **gov_lineage
                    )
                except Exception:
                    pass
        return provider_id, result, elapsed_ms

    async def run(
        self,
        prompt: str,
        selected=None,
        task_id=None,
        agent_id=None,
        workflow_id=None,
        request_id=None,
        tenant_id=None,
        user_id=None,
        actor_ref=None,
        envelope=None,
    ):

        if selected is None:
            available = [
                (provider_id, self.get_provider(provider_id))
                for provider_id in PROVIDER_IDS
                if self.get_provider(provider_id) is not None
            ]
        else:
            available = list(selected)

        # Envelope wins when present; else legacy kwargs. Never re-read self.last_*
        # after await for attribution / reservations / reconcile / release.
        if envelope is not None:
            run_task_id = envelope.task_id
            run_workflow_id = envelope.workflow_id
            run_request_id = envelope.request_id
            run_tenant_id = envelope.tenant_id
            run_user_id = envelope.user_id
            run_actor_ref = envelope.actor_ref
            run_execution_id = envelope.execution_id
        else:
            run_task_id = task_id or str(uuid.uuid4())
            run_workflow_id = workflow_id
            run_request_id = request_id
            run_tenant_id = tenant_id
            run_user_id = user_id
            run_actor_ref = actor_ref
            run_execution_id = ""
        run_reservations: dict = {}
        run_errors: dict = {}
        run_provider_results: dict = {}
        run_usage: list = []
        run_guard_decision = None
        run_budget_exceeded = False
        provider_calls = 0
        self.last_run_envelope = envelope

        # Diagnostic snapshot only (not source of truth for this execution).
        self.last_task_id = run_task_id
        self.last_workflow_id = run_workflow_id
        self.last_request_id = run_request_id
        self.last_tenant_id = run_tenant_id
        self.last_user_id = run_user_id
        self.last_actor_ref = run_actor_ref
        self.last_budget_exceeded = False
        self.last_guard_decision = None
        self.provider_calls = 0

        if not available:
            self.last_errors = {}
            self.last_provider_results = {}
            self.last_usage = []
            self.last_reservations = {}
            return {}

        guard = self.budget_guard
        use_guard = guard is not None and getattr(guard, "enforcement_active", False)
        identity_kw = dict(
            workflow_id=run_workflow_id,
            task_id=run_task_id,
            request_id=run_request_id,
            tenant_id=run_tenant_id,
            actor_ref=run_actor_ref,
        )
        # Request-local budget obs lineage (envelope wins; never mutate envelope).
        budget_lineage = {}
        if envelope is not None:
            budget_lineage["envelope"] = envelope
        else:
            parent_ctx = self._attribution_obs_context(
                workflow_id=run_workflow_id,
                task_id=run_task_id,
                request_id=run_request_id,
                tenant_id=run_tenant_id,
                actor_ref=run_actor_ref,
            )
            if parent_ctx is not None:
                budget_lineage["parent_context"] = parent_ctx
            if run_workflow_id:
                budget_lineage["workflow_id"] = run_workflow_id
            if run_actor_ref:
                budget_lineage["actor_ref"] = run_actor_ref

        if use_guard:
            from finops.budget_guard import BudgetGuardError
            from finops.budget_models import (
                DECISION_DEGRADE,
                DECISION_SKIP_MODEL,
                DECISION_TERMINATE,
            )

            if guard.observability is None and self.observability is not None:
                guard.observability = self.observability

            # Soft DEGRADE needs the current selected/available set to pick a
            # cheaper capable route. Same tuple must be passed to evaluate+reserve
            # (reserve re-evaluates under lock).
            capable_candidates = tuple(
                (str(provider_id), str(getattr(agent, "model", "") or ""))
                for provider_id, agent in available
            )

            runnable = []
            last_deny_reason = None
            for provider_id, agent in available:
                # Sticky global hard violation: no further paid execution permitted.
                if getattr(guard, "_hard_violation", False):
                    raise FinOpsBudgetDeniedError("budget_hard_limit_exceeded")
                model_id = getattr(agent, "model", "") or ""
                estimated = guard.estimate_request_cost(provider_id, model_id)
                decision = guard.evaluate(
                    task_id=run_task_id,
                    provider=provider_id,
                    model=model_id,
                    estimated_cost=estimated,
                    agent_id=agent_id,
                    tenant_id=run_tenant_id,
                    capable_candidates=capable_candidates,
                    **budget_lineage,
                )
                run_guard_decision = decision
                self.last_guard_decision = decision
                if decision.decision == DECISION_TERMINATE:
                    # Global deny remains terminal. Provider-specific unaffordability
                    # is SKIP_MODEL (below) and must not abort affordable peers.
                    if getattr(guard, "_hard_violation", False):
                        raise FinOpsBudgetDeniedError(decision.reason_code)
                    if decision.reason_code == "budget_tenant_required":
                        raise FinOpsBudgetDeniedError(decision.reason_code)
                    # TERMINATE with no affordable route (e.g. DEGRADE collapsed).
                    last_deny_reason = decision.reason_code
                    continue
                if decision.decision == DECISION_SKIP_MODEL:
                    last_deny_reason = decision.reason_code
                    continue
                if decision.decision == DECISION_DEGRADE:
                    if provider_id in decision.excluded_providers:
                        continue
                    if (
                        decision.recommended_provider
                        and provider_id != decision.recommended_provider
                    ):
                        continue
                if estimated is None:
                    # Never treat unknown as free; cannot reserve without estimate.
                    # Skip this provider only — do not abort other known-cost peers.
                    last_deny_reason = "unknown_cost_cannot_reserve"
                    continue
                try:
                    reservation = guard.reserve(
                        task_id=run_task_id,
                        provider=provider_id,
                        model=model_id,
                        estimated_cost=estimated,
                        agent_id=agent_id,
                        tenant_id=run_tenant_id,
                        capable_candidates=capable_candidates,
                        **budget_lineage,
                    )
                except BudgetGuardError as exc:
                    if getattr(guard, "_hard_violation", False):
                        raise FinOpsBudgetDeniedError(exc.reason) from exc
                    last_deny_reason = exc.reason
                    continue
                run_reservations[provider_id] = reservation
                runnable.append((provider_id, agent))
            available = runnable
            if not available:
                raise FinOpsBudgetDeniedError(
                    last_deny_reason or "budget_no_affordable_capable_route"
                )
        elif self.finops is not None:
            decision = self.finops.check_budget(None)
            self.last_budget_decision = decision
            if not decision.allowed:
                raise FinOpsBudgetDeniedError(decision.reason)

        if (
            self.provider_governor is not None
            and getattr(self.provider_governor, "observability", None) is None
            and self.observability is not None
        ):
            self.provider_governor.observability = self.observability

        timed = await asyncio.gather(
            *[
                self._run_provider_timed(
                    provider_id,
                    agent,
                    prompt,
                    envelope=envelope,
                    parent_context=budget_lineage.get("parent_context"),
                    workflow_id=run_workflow_id,
                    task_id=run_task_id,
                    tenant_id=run_tenant_id,
                    actor_ref=run_actor_ref,
                    request_id=run_request_id,
                )
                for provider_id, agent in available
            ]
        )
        provider_calls = sum(
            1 for _pid, result, _ms in timed if not isinstance(result, BaseException)
        )

        experts = {}
        by_id = {provider_id: agent for provider_id, agent in available}

        for provider_id, result, latency_ms in timed:
            agent = by_id[provider_id]
            reservation = run_reservations.get(provider_id)
            model_id = getattr(agent, "model", "") or ""
            self._record_health_outcome(
                provider_id=provider_id,
                model_id=model_id,
                result=result,
                latency_ms=latency_ms,
                **identity_kw,
            )
            if isinstance(result, BaseException):
                self._record_runtime_outcome(
                    provider_id=provider_id,
                    model_id=model_id,
                    result=result,
                    latency_ms=latency_ms,
                    cost=None,
                )
                run_errors[provider_id] = {
                    "type": type(result).__name__,
                    "message": redact(str(result)),
                }
                if reservation is not None and guard is not None:
                    name = type(result).__name__
                    if "Timeout" in name:
                        guard.reconcile(
                            reservation.reservation_id,
                            actual_cost=None,
                            uncertain=True,
                            **budget_lineage,
                        )
                    else:
                        guard.release(reservation.reservation_id, **budget_lineage)
                continue

            normalized = self._normalize_result(provider_id, agent, result)
            run_provider_results[provider_id] = normalized
            experts[provider_id] = normalized.text
            record = self._record_usage(
                normalized,
                task_id=run_task_id,
                workflow_id=run_workflow_id,
                tenant_id=run_tenant_id,
                user_id=run_user_id,
                request_id=run_request_id,
                actor_ref=run_actor_ref if envelope is not None else "",
                execution_id=run_execution_id,
                usage_out=run_usage,
            )
            if record is not None:
                run_budget_exceeded = self.finops.is_over_limit(
                    when=record.timestamp,
                    task_id=run_task_id,
                )
            actual_cost = record.estimated_cost if record is not None else None
            self._record_runtime_outcome(
                provider_id=provider_id,
                model_id=model_id,
                result=result,
                latency_ms=latency_ms,
                cost=actual_cost,
            )
            if reservation is not None and guard is not None:
                if actual_cost is None:
                    guard.reconcile(
                        reservation.reservation_id,
                        actual_cost=None,
                        uncertain=True,
                        **budget_lineage,
                    )
                else:
                    guard.reconcile(
                        reservation.reservation_id,
                        actual_cost=actual_cost,
                        usage_record_key=(
                            f"{record.task_id}:{record.provider_id}:"
                            f"{record.timestamp.isoformat()}"
                        ),
                        **budget_lineage,
                    )

        # Best-effort diagnostic snapshot after this execution completes.
        self.last_errors = dict(run_errors)
        self.last_provider_results = dict(run_provider_results)
        self.last_usage = list(run_usage)
        self.last_reservations = dict(run_reservations)
        self.last_guard_decision = run_guard_decision
        self.last_budget_exceeded = run_budget_exceeded
        self.provider_calls = provider_calls
        self.last_task_id = run_task_id
        self.last_workflow_id = run_workflow_id
        self.last_request_id = run_request_id
        self.last_tenant_id = run_tenant_id
        self.last_user_id = run_user_id
        self.last_actor_ref = run_actor_ref

        return experts
