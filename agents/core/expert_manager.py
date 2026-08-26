import asyncio
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
        self.last_errors = {}
        self.last_provider_results = {}
        self.last_usage = []
        self.last_task_id = None
        self.last_budget_exceeded = False
        self.last_budget_decision = None
        self.last_guard_decision = None
        self.last_reservations = {}
        self.provider_calls = 0

    def get_provider(self, provider_id: str):
        if provider_id not in PROVIDER_IDS:
            return None
        return getattr(self, provider_id)

    def _normalize_result(self, provider_id, agent, result) -> ProviderResult:
        if isinstance(result, ProviderResult):
            return result
        model_id = getattr(agent, "model", "") or ""
        if isinstance(result, str):
            return provider_result_from_text(provider_id, model_id, result)
        return provider_result_from_text(provider_id, model_id, str(result))

    def _record_usage(self, result: ProviderResult) -> UsageRecord | None:
        if self.finops is None:
            return None
        estimated_cost = self.finops.estimate(
            result.provider_id,
            result.model_id,
            result.input_tokens,
            result.output_tokens,
        )
        quote = self.finops.quote(result.provider_id, result.model_id)
        record = UsageRecord(
            task_id=self.last_task_id,
            provider_id=result.provider_id,
            model_id=result.model_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            estimated_cost=estimated_cost,
            currency=quote.currency if quote else CURRENCY_USD,
            timestamp=datetime.now(timezone.utc),
        )
        self.finops.record_usage(record)
        self.last_usage.append(record)
        self.last_budget_exceeded = self.finops.is_over_limit(
            when=record.timestamp,
            task_id=self.last_task_id,
        )
        return record

    async def run(self, prompt: str, selected=None, task_id=None, agent_id=None):

        if selected is None:
            available = [
                (provider_id, self.get_provider(provider_id))
                for provider_id in PROVIDER_IDS
                if self.get_provider(provider_id) is not None
            ]
        else:
            available = list(selected)

        self.last_errors = {}
        self.last_provider_results = {}
        self.last_usage = []
        self.last_budget_exceeded = False
        self.last_reservations = {}
        self.last_guard_decision = None
        self.provider_calls = 0
        self.last_task_id = task_id or str(uuid.uuid4())

        if not available:
            return {}

        guard = self.budget_guard
        use_guard = guard is not None and getattr(guard, "enforcement_active", False)

        if use_guard:
            from finops.budget_guard import BudgetGuardError
            from finops.budget_models import DECISION_DEGRADE, DECISION_TERMINATE

            runnable = []
            for provider_id, agent in available:
                model_id = getattr(agent, "model", "") or ""
                estimated = guard.estimate_request_cost(provider_id, model_id)
                decision = guard.evaluate(
                    task_id=self.last_task_id,
                    provider=provider_id,
                    model=model_id,
                    estimated_cost=estimated,
                    agent_id=agent_id,
                )
                self.last_guard_decision = decision
                if decision.decision == DECISION_TERMINATE:
                    raise FinOpsBudgetDeniedError(decision.reason_code)
                if decision.decision == DECISION_DEGRADE:
                    if provider_id in decision.excluded_providers:
                        continue
                    if (
                        decision.recommended_provider
                        and provider_id != decision.recommended_provider
                    ):
                        continue
                if estimated is None:
                    # Never treat unknown as free; unknown policy already applied in evaluate.
                    # Cannot reserve without a positive estimate.
                    raise FinOpsBudgetDeniedError("unknown_cost_cannot_reserve")
                try:
                    reservation = guard.reserve(
                        task_id=self.last_task_id,
                        provider=provider_id,
                        model=model_id,
                        estimated_cost=estimated,
                        agent_id=agent_id,
                    )
                except BudgetGuardError as exc:
                    raise FinOpsBudgetDeniedError(exc.reason) from exc
                self.last_reservations[provider_id] = reservation
                runnable.append((provider_id, agent))
            available = runnable
            if not available:
                raise FinOpsBudgetDeniedError("budget_no_affordable_capable_route")
        elif self.finops is not None:
            decision = self.finops.check_budget(None)
            self.last_budget_decision = decision
            if not decision.allowed:
                raise FinOpsBudgetDeniedError(decision.reason)

        results = await asyncio.gather(
            *[agent.run(prompt) for _, agent in available],
            return_exceptions=True,
        )
        self.provider_calls = sum(
            1 for result in results if not isinstance(result, BaseException)
        )

        experts = {}

        for (provider_id, agent), result in zip(available, results):
            reservation = self.last_reservations.get(provider_id)
            if isinstance(result, BaseException):
                self.last_errors[provider_id] = {
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
                        )
                    else:
                        guard.release(reservation.reservation_id)
                continue

            normalized = self._normalize_result(provider_id, agent, result)
            self.last_provider_results[provider_id] = normalized
            experts[provider_id] = normalized.text
            record = self._record_usage(normalized)
            if reservation is not None and guard is not None:
                actual = record.estimated_cost if record is not None else None
                if actual is None:
                    guard.reconcile(
                        reservation.reservation_id,
                        actual_cost=None,
                        uncertain=True,
                    )
                else:
                    guard.reconcile(
                        reservation.reservation_id,
                        actual_cost=actual,
                        usage_record_key=(
                            f"{record.task_id}:{record.provider_id}:"
                            f"{record.timestamp.isoformat()}"
                        ),
                    )

        return experts
