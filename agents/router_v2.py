from agents.openai_agent import OpenAIAgent
from agents.anthropic_agent import AnthropicAgent
from agents.gemini_agent import GeminiAgent
from agents.grok_agent import GrokAgent
from agents.deepseek_agent import DeepSeekAgent

from agents.peer_review import PeerReview
from agents.fact_validator import FactValidator
from agents.judge import Judge

from agents.core.pipeline import Pipeline
from agents.core.expert_manager import ExpertManager
from agents.core.response_formatter import ResponseFormatter
from agents.core.decision_memory import DecisionMemory
from agents.core.supervisor import Supervisor
from agents.role_registry import (
    ALLOWED_ROLE_VALUES,
    DEFAULT_ROLE,
    compose_prompt,
    get_role_prompt,
)
from agents.provider_registry import PROVIDER_IDS, ProviderRegistry
from agents.model_router import ModelRouter
from agents.task_classifier import TaskClassifier


ALLOWED_MODE_VALUES = (
    "openai",
    "anthropic",
    "gemini",
    "grok",
    "deepseek",
    "both",
    "auto",
)

ALLOWED_MODES = frozenset(ALLOWED_MODE_VALUES)

ROLE_AUTO = "auto"

ALLOWED_API_ROLE_VALUES = ALLOWED_ROLE_VALUES + (ROLE_AUTO,)

PROVIDER_CLASSES = {
    "openai": OpenAIAgent,
    "anthropic": AnthropicAgent,
    "gemini": GeminiAgent,
    "grok": GrokAgent,
    "deepseek": DeepSeekAgent,
}


class InvalidModeError(ValueError):
    def __init__(self, mode):
        self.mode = mode
        super().__init__(f"Invalid mode: {mode!r}")


class ProviderNotConfiguredError(Exception):
    def __init__(self, provider: str, mode: str):
        self.provider = provider
        self.mode = mode
        super().__init__(f"Provider {provider} is not configured.")


class NoProvidersAvailableError(Exception):
    pass


class RouterV2:
    """
    Panda Multi-Agent V2
    """

    def __init__(self):
        registry = ProviderRegistry.from_env()
        self.provider_registry = registry
        self.model_router = ModelRouter(registry)

        expert_manager = ExpertManager(
            **{
                provider_id: (
                    PROVIDER_CLASSES[provider_id]()
                    if registry.is_available(provider_id)
                    else None
                )
                for provider_id in PROVIDER_IDS
            }
        )

        self.pipeline = Pipeline(
            expert_manager=expert_manager,
            peer_review=PeerReview(),
            fact_validator=FactValidator(),
            judge=Judge(),
            response_formatter=ResponseFormatter(),
            supervisor=Supervisor(),
            decision_memory=DecisionMemory(),
        )
        self.last_decision = None
        self.last_classification = None
        self.task_classifier = TaskClassifier()

    def provider_status(self) -> dict:
        return self.provider_registry.status()

    def has_available_providers(self) -> bool:
        return any(self.provider_status().values())

    def _agents_for_decision(self, decision):
        manager = self.pipeline.expert_manager
        selected = []
        for provider_id in decision.provider_ids:
            agent = manager.get_provider(provider_id)
            selected.append((provider_id, agent))
        return selected

    async def run(
        self,
        prompt: str,
        mode: str | None = None,
        role: str | None = None,
    ):
        if mode is None:
            resolved_mode = "both"
        else:
            resolved_mode = mode

        if resolved_mode not in ALLOWED_MODES:
            raise InvalidModeError(mode)

        requested_role = DEFAULT_ROLE if role is None else role
        self.last_classification = None

        if requested_role == ROLE_AUTO:
            self.last_classification = self.task_classifier.classify(prompt)
            resolved_role = self.last_classification.role_id
        else:
            resolved_role = requested_role

        get_role_prompt(resolved_role)

        decision = self.model_router.decide(
            mode=resolved_mode,
            role_id=resolved_role,
        )
        self.last_decision = decision

        composed = compose_prompt(decision.role_id, prompt)
        selected = self._agents_for_decision(decision)

        if decision.reason == "explicit_provider":
            provider_id = decision.provider_ids[0]
            agent = selected[0][1]
            if agent is None:
                raise ProviderNotConfiguredError(
                    provider=provider_id,
                    mode=resolved_mode,
                )
            return await self.pipeline.execute(
                composed,
                selected=[(provider_id, agent)],
            )

        if not selected or any(agent is None for _, agent in selected):
            raise NoProvidersAvailableError()

        return await self.pipeline.execute(composed, selected=selected)
