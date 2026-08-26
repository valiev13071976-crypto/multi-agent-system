import uuid

from agents.openai_agent import OpenAIAgent
from agents.anthropic_agent import AnthropicAgent
from agents.gemini_agent import GeminiAgent
from agents.grok_agent import GrokAgent
from agents.deepseek_agent import DeepSeekAgent
from agents.moonshot_agent import MoonshotAgent
from agents.mistral_agent import MistralAgent

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
from agents.model_profile import routing_category_for_role
from agents.model_router import ModelRouter
from agents.task_classifier import TaskClassifier
from agents.routing_requirements import derive_task_requirements
from agents.routing_health import ProviderHealthTracker, load_routing_health_policy
from config.pricing import (
    load_budget_guard_enabled,
    load_budget_limits,
    load_price_quotes,
)
from finops.service import FinOpsService
from tools.gateway import ToolGateway
from tools.search.null_provider import NullSearchProvider
from workflow.engine import WorkflowEngine


ALLOWED_MODE_VALUES = (
    "openai",
    "anthropic",
    "gemini",
    "grok",
    "deepseek",
    "moonshot",
    "mistral",
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
    "moonshot": MoonshotAgent,
    "mistral": MistralAgent,
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
        health_policy = load_routing_health_policy()
        self.health_tracker = (
            ProviderHealthTracker(policy=health_policy)
            if health_policy.enabled
            else None
        )
        self.model_router = ModelRouter(registry, health_tracker=self.health_tracker)

        self.finops = FinOpsService(
            prices=load_price_quotes(),
            limits=load_budget_limits(),
        )
        self.budget_guard = None
        if load_budget_guard_enabled():
            from finops.budget_guard import BudgetGuard
            from finops.budget_policy import load_advanced_budget_policies

            self.budget_guard = BudgetGuard(
                finops=self.finops,
                policies=load_advanced_budget_policies(limits=self.finops._limits),
                required=True,
            )

        expert_manager = ExpertManager(
            **{
                provider_id: (
                    PROVIDER_CLASSES[provider_id]()
                    if registry.is_available(provider_id)
                    else None
                )
                for provider_id in PROVIDER_IDS
            },
            finops=self.finops,
            budget_guard=self.budget_guard,
        )
        expert_manager.health_tracker = self.health_tracker

        self.tool_gateway = ToolGateway(NullSearchProvider())

        self.pipeline = Pipeline(
            expert_manager=expert_manager,
            peer_review=PeerReview(),
            fact_validator=FactValidator(gateway=self.tool_gateway),
            judge=Judge(),
            response_formatter=ResponseFormatter(),
            supervisor=Supervisor(),
            decision_memory=DecisionMemory(),
        )
        self.last_decision = None
        self.last_classification = None
        self.last_requirements = None
        self.last_route_context = None
        self.last_task_id = None
        self.last_workflow_id = None
        self.task_classifier = TaskClassifier()
        self.workflow_engine = WorkflowEngine()

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
        task_id: str | None = None,
        lifecycle=None,
    ):
        if mode is None:
            resolved_mode = "both"
        else:
            resolved_mode = mode

        started_route = False
        try:
            if lifecycle is not None:
                started_route = await lifecycle.begin("route")

            if resolved_mode not in ALLOWED_MODES:
                raise InvalidModeError(mode)

            self.last_task_id = task_id or self.last_task_id or str(uuid.uuid4())
            if lifecycle is not None:
                self.last_workflow_id = lifecycle.workflow_id

            requested_role = DEFAULT_ROLE if role is None else role
            self.last_classification = None
            self.last_requirements = None
            self.last_route_context = None

            if requested_role == ROLE_AUTO:
                self.last_classification = self.task_classifier.classify(prompt)
                resolved_role = self.last_classification.role_id
                routing_category = self.last_classification.category
                category_source = "classifier"
                self.last_requirements = self.last_classification.requirements
            else:
                resolved_role = requested_role
                routing_category = routing_category_for_role(resolved_role)
                category_source = "role_mapping"
                self.last_requirements = derive_task_requirements(
                    category=routing_category,
                    text=prompt,
                )

            get_role_prompt(resolved_role)

            self.last_route_context = {
                "category": routing_category,
                "source": category_source,
                "policy": self.provider_registry.auto_routing_policy,
                "requirements": (
                    dict(self.last_requirements.as_dict())
                    if self.last_requirements is not None
                    else None
                ),
            }

            budget_constraints = None
            # mode=both: no routing-time budget filter (fan-out preserved;
            # execution-time BudgetGuard remains authoritative).
            if (
                resolved_mode != "both"
                and self.budget_guard is not None
                and self.budget_guard.enforcement_active
            ):
                candidates = tuple(
                    (provider_id, self.provider_registry.model(provider_id))
                    for provider_id in self.provider_registry.available_provider_ids()
                )
                budget_constraints = self.budget_guard.routing_constraints(
                    task_id=self.last_task_id,
                    candidates=candidates,
                )

            decision = self.model_router.decide(
                mode=resolved_mode,
                role_id=resolved_role,
                category=routing_category,
                requirements=self.last_requirements,
                budget_constraints=budget_constraints,
            )
            self.last_decision = decision

            composed = compose_prompt(decision.role_id, prompt)
            selected = self._agents_for_decision(decision)

            if lifecycle is not None and started_route:
                await lifecycle.end(
                    "route",
                    metadata={
                        "reason": decision.reason,
                        "provider_count": len(decision.provider_ids),
                    },
                )

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
                    task_id=self.last_task_id,
                    category=routing_category,
                    lifecycle=lifecycle,
                )

            if not selected or any(agent is None for _, agent in selected):
                raise NoProvidersAvailableError()

            return await self.pipeline.execute(
                composed,
                selected=selected,
                task_id=self.last_task_id,
                category=routing_category,
                lifecycle=lifecycle,
            )
        except Exception as exc:
            if lifecycle is not None:
                from workflow.engine import error_code_for
                from workflow.models import STEP_COMPLETED, STEP_ROUTE

                state = lifecycle.manager.get(lifecycle.workflow_id)
                route = state.step(STEP_ROUTE)
                if route is None or route.status != STEP_COMPLETED:
                    await lifecycle.fail(STEP_ROUTE, error_code_for(exc))
            raise
