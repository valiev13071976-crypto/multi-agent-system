from agents.validators.consistency import ConsistencyValidator
from agents.validators.structural import StructuralValidator


class Pipeline:
    """
    Главный конвейер Panda Multi-Agent V2.
    """

    def __init__(
        self,
        expert_manager,
        peer_review,
        fact_validator,
        judge,
        response_formatter,
        supervisor,
        decision_memory,
        structural_validator=None,
        consistency_validator=None,
    ):
        self.expert_manager = expert_manager
        self.peer_review = peer_review
        self.fact_validator = fact_validator
        self.judge = judge
        self.response_formatter = response_formatter
        self.supervisor = supervisor
        self.decision_memory = decision_memory
        self.structural_validator = structural_validator or StructuralValidator()
        self.consistency_validator = consistency_validator or ConsistencyValidator()
        self.last_validation = None

    async def execute(self, prompt: str, selected=None, task_id=None, category=None):

        experts = await self.expert_manager.run(
            prompt,
            selected=selected,
            task_id=task_id,
        )
        provider_errors = getattr(self.expert_manager, "last_errors", {}) or {}
        gateway = getattr(self.fact_validator, "gateway", None)
        if gateway is not None:
            gateway.task_id = task_id or ""
            if hasattr(gateway, "reset_budget"):
                gateway.reset_budget()

        structural = self.structural_validator.validate_experts(experts)
        peer = await self.peer_review.review(experts, errors=provider_errors)
        consistency = self.consistency_validator.validate(experts)
        facts = await self.fact_validator.validate(
            experts,
            category=category,
        )

        self.last_validation = {
            "structural": structural,
            "peer_review": peer,
            "consistency": consistency,
            "fact": facts,
            "category": category,
        }

        decision = await self.judge.run(
            experts=experts,
            peer_review=peer,
            fact_report=facts,
            structural=structural,
            consistency=consistency,
            provider_errors=provider_errors,
            category=category,
        )

        answer = await self.response_formatter.format(
            decision
        )

        await self.decision_memory.save(
            prompt,
            answer,
        )

        return answer
