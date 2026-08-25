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
        finops=None,
    ):
        self.openai = openai
        self.anthropic = anthropic
        self.gemini = gemini
        self.grok = grok
        self.deepseek = deepseek
        self.finops = finops
        self.last_errors = {}
        self.last_provider_results = {}
        self.last_usage = []
        self.last_task_id = None
        self.last_budget_exceeded = False
        self.last_budget_decision = None

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

    def _record_usage(self, result: ProviderResult) -> None:
        if self.finops is None:
            return
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

    async def run(self, prompt: str, selected=None, task_id=None):

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
        self.last_task_id = task_id or str(uuid.uuid4())

        if not available:
            return {}

        if self.finops is not None:
            decision = self.finops.check_budget(None)
            self.last_budget_decision = decision
            if not decision.allowed:
                raise FinOpsBudgetDeniedError(decision.reason)

        results = await asyncio.gather(
            *[agent.run(prompt) for _, agent in available],
            return_exceptions=True,
        )

        experts = {}

        for (provider_id, agent), result in zip(available, results):
            if isinstance(result, BaseException):
                self.last_errors[provider_id] = {
                    "type": type(result).__name__,
                    "message": redact(str(result)),
                }
                continue

            normalized = self._normalize_result(provider_id, agent, result)
            self.last_provider_results[provider_id] = normalized
            experts[provider_id] = normalized.text
            self._record_usage(normalized)

        return experts
