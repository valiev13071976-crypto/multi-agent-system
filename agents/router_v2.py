import os

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
    DEFAULT_ROLE,
    compose_prompt,
    get_role_prompt,
)


ALLOWED_MODE_VALUES = (
    "openai",
    "anthropic",
    "gemini",
    "grok",
    "deepseek",
    "both",
)

ALLOWED_MODES = frozenset(ALLOWED_MODE_VALUES)

PROVIDER_IDS = (
    "openai",
    "anthropic",
    "gemini",
    "grok",
    "deepseek",
)


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


def _maybe_agent(agent_cls, key_env: str, model_env: str):
    if os.getenv(key_env) and os.getenv(model_env):
        return agent_cls()
    return None


class RouterV2:
    """
    Panda Multi-Agent V2
    """

    def __init__(self):

        expert_manager = ExpertManager(
            openai=_maybe_agent(
                OpenAIAgent, "OPENAI_API_KEY", "OPENAI_MODEL"
            ),
            anthropic=_maybe_agent(
                AnthropicAgent, "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"
            ),
            gemini=_maybe_agent(
                GeminiAgent, "GEMINI_API_KEY", "GEMINI_MODEL"
            ),
            grok=_maybe_agent(
                GrokAgent, "XAI_API_KEY", "XAI_MODEL"
            ),
            deepseek=_maybe_agent(
                DeepSeekAgent, "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"
            ),
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

    def provider_status(self) -> dict:
        manager = self.pipeline.expert_manager
        return {
            provider_id: manager.get_provider(provider_id) is not None
            for provider_id in PROVIDER_IDS
        }

    def has_available_providers(self) -> bool:
        return any(self.provider_status().values())

    def _available_agents(self):
        manager = self.pipeline.expert_manager
        selected = []
        for provider_id in PROVIDER_IDS:
            agent = manager.get_provider(provider_id)
            if agent is not None:
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

        resolved_role = DEFAULT_ROLE if role is None else role
        get_role_prompt(resolved_role)

        composed = compose_prompt(resolved_role, prompt)

        if resolved_mode == "both":
            selected = self._available_agents()
            if not selected:
                raise NoProvidersAvailableError()
            return await self.pipeline.execute(composed, selected=selected)

        agent = self.pipeline.expert_manager.get_provider(resolved_mode)
        if agent is None:
            raise ProviderNotConfiguredError(
                provider=resolved_mode,
                mode=resolved_mode,
            )

        return await self.pipeline.execute(
            composed,
            selected=[(resolved_mode, agent)],
        )
