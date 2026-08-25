from agents.validators.consistency import ConsistencyValidator
from agents.validators.structural import StructuralValidator
from workflow.engine import error_code_for
from workflow.models import (
    STEP_EXECUTE_EXPERTS,
    STEP_FORMAT,
    STEP_JUDGE,
    STEP_VALIDATE,
)


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

    async def _begin(self, lifecycle, name):
        if lifecycle is None:
            return True
        return await lifecycle.begin(name)

    async def _end(self, lifecycle, name, metadata=None):
        if lifecycle is None:
            return
        await lifecycle.end(name, metadata=metadata)

    async def _fail(self, lifecycle, name, exc):
        if lifecycle is None:
            return
        await lifecycle.fail(name, error_code_for(exc))

    async def execute(
        self,
        prompt: str,
        selected=None,
        task_id=None,
        category=None,
        lifecycle=None,
    ):

        try:
            await self._begin(lifecycle, STEP_EXECUTE_EXPERTS)
            experts = await self.expert_manager.run(
                prompt,
                selected=selected,
                task_id=task_id,
            )
            provider_errors = getattr(self.expert_manager, "last_errors", {}) or {}
            await self._end(
                lifecycle,
                STEP_EXECUTE_EXPERTS,
                metadata={
                    "expert_count": len(experts),
                    "failed_count": len(provider_errors),
                },
            )
        except Exception as exc:
            await self._fail(lifecycle, STEP_EXECUTE_EXPERTS, exc)
            raise

        gateway = getattr(self.fact_validator, "gateway", None)
        if gateway is not None:
            gateway.task_id = task_id or ""
            if hasattr(gateway, "reset_budget"):
                gateway.reset_budget()

        try:
            await self._begin(lifecycle, STEP_VALIDATE)
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
            await self._end(
                lifecycle,
                STEP_VALIDATE,
                metadata={
                    "fact_status": getattr(facts, "status", ""),
                    "fact_reason": getattr(facts, "reason", ""),
                },
            )
        except Exception as exc:
            await self._fail(lifecycle, STEP_VALIDATE, exc)
            raise

        try:
            await self._begin(lifecycle, STEP_JUDGE)
            decision = await self.judge.run(
                experts=experts,
                peer_review=peer,
                fact_report=facts,
                structural=structural,
                consistency=consistency,
                provider_errors=provider_errors,
                category=category,
            )
            await self._end(lifecycle, STEP_JUDGE)
        except Exception as exc:
            await self._fail(lifecycle, STEP_JUDGE, exc)
            raise

        try:
            await self._begin(lifecycle, STEP_FORMAT)
            answer = await self.response_formatter.format(
                decision
            )
            await self.decision_memory.save(
                prompt,
                answer,
            )
            await self._end(lifecycle, STEP_FORMAT)
        except Exception as exc:
            await self._fail(lifecycle, STEP_FORMAT, exc)
            raise

        return answer
