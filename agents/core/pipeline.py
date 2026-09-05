from agents.validators.consistency import ConsistencyValidator
from agents.validators.structural import StructuralValidator
from agents.execution_policy import POLICY_FULL, POLICY_LIGHTWEIGHT, POLICY_STANDARD
from agents.validators.models import STATUS_UNKNOWN, PeerReviewResult, ValidationResult
from workflow.engine import error_code_for
from workflow.models import (
    STEP_EXECUTE_EXPERTS,
    STEP_FORMAT,
    STEP_JUDGE,
    STEP_VALIDATE,
)
import time


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
        self.last_execution_policy = None
        self.last_stages_invoked = ()
        self.last_latency_ms = {}

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
        workflow_id=None,
        request_id=None,
        tenant_id=None,
        user_id=None,
        actor_ref=None,
        envelope=None,
        execution_policy: str | None = None,
    ):
        policy = execution_policy or POLICY_FULL
        self.last_execution_policy = policy
        stages: list[str] = []
        latency: dict[str, int] = {}
        t0 = time.monotonic()

        def _mark(name: str, started: float) -> None:
            latency[name] = int((time.monotonic() - started) * 1000)

        try:
            await self._begin(lifecycle, STEP_EXECUTE_EXPERTS)
            experts = await self.expert_manager.run(
                prompt,
                selected=selected,
                task_id=task_id,
                workflow_id=workflow_id,
                request_id=request_id,
                tenant_id=tenant_id,
                user_id=user_id,
                actor_ref=actor_ref,
                envelope=envelope,
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
            stages.append("experts")
            _mark("provider_ms", t0)
        except Exception as exc:
            await self._fail(lifecycle, STEP_EXECUTE_EXPERTS, exc)
            raise

        gateway = getattr(self.fact_validator, "gateway", None)
        if gateway is not None and policy == POLICY_FULL:
            if envelope is not None:
                gateway.task_id = envelope.task_id or ""
            else:
                gateway.task_id = task_id or ""
            if hasattr(gateway, "reset_budget"):
                gateway.reset_budget()

        peer = None
        facts = None
        structural = None
        consistency = None

        try:
            await self._begin(lifecycle, STEP_VALIDATE)
            v0 = time.monotonic()
            if policy == POLICY_LIGHTWEIGHT:
                structural = {}
                peer = PeerReviewResult(
                    validator_id="peer_review",
                    status=STATUS_UNKNOWN,
                    score=0.0,
                    issues=(),
                    evidence={"skipped": True, "policy": POLICY_LIGHTWEIGHT},
                    reason="lightweight_skipped",
                    answered_provider_ids=tuple(sorted(str(k) for k in (experts or {}))),
                    failed_provider_ids=tuple(sorted(str(k) for k in (provider_errors or {}))),
                )
                consistency = None
                facts = ValidationResult(
                    validator_id="fact",
                    status=STATUS_UNKNOWN,
                    score=0.0,
                    issues=(),
                    evidence={"skipped": True, "policy": POLICY_LIGHTWEIGHT},
                    reason="lightweight_skipped",
                )
            else:
                structural = self.structural_validator.validate_experts(experts)
                peer = await self.peer_review.review(experts, errors=provider_errors)
                stages.append("peer_review")
                consistency = self.consistency_validator.validate(experts)
                if envelope is not None:
                    run_workflow_id = envelope.workflow_id
                    run_task_id = envelope.task_id
                    run_tenant_id = envelope.tenant_id
                    run_actor_ref = envelope.actor_ref
                    run_request_id = envelope.request_id
                else:
                    run_workflow_id = workflow_id or (
                        lifecycle.workflow_id if lifecycle is not None else None
                    )
                    run_task_id = task_id
                    run_tenant_id = tenant_id
                    run_actor_ref = actor_ref
                    run_request_id = request_id
                parent_context = None
                if envelope is None:
                    obs = getattr(self.fact_validator, "observability", None)
                    if obs is None and gateway is not None:
                        obs = getattr(gateway, "observability", None)
                    if obs is not None and run_workflow_id:
                        parent_context = obs.context_for_workflow(run_workflow_id)
                if policy == POLICY_FULL:
                    facts = await self.fact_validator.validate(
                        experts,
                        category=category,
                        envelope=envelope,
                        parent_context=parent_context,
                        task_id=run_task_id,
                        workflow_id=run_workflow_id,
                        tenant_id=run_tenant_id,
                        actor_ref=run_actor_ref,
                        request_id=run_request_id,
                    )
                    stages.append("fact_validator")
                else:
                    facts = ValidationResult(
                        validator_id="fact",
                        status=STATUS_UNKNOWN,
                        score=0.0,
                        issues=(),
                        evidence={"skipped": True, "policy": POLICY_STANDARD},
                        reason="standard_fact_skipped",
                    )
            self.last_validation = {
                "structural": structural,
                "peer_review": peer,
                "consistency": consistency,
                "fact": facts,
                "category": category,
            }
            _mark("validation_ms", v0)
            await self._end(
                lifecycle,
                STEP_VALIDATE,
                metadata={
                    "fact_status": getattr(facts, "status", ""),
                    "fact_reason": getattr(facts, "reason", ""),
                    "execution_policy": policy,
                },
            )
        except Exception as exc:
            await self._fail(lifecycle, STEP_VALIDATE, exc)
            raise

        try:
            await self._begin(lifecycle, STEP_JUDGE)
            j0 = time.monotonic()
            if policy == POLICY_LIGHTWEIGHT:
                decision = {
                    "role": "Judge",
                    "summary": "",
                    "best_solution": "",
                    "final_answer": self.judge._user_facing_from_experts(experts),
                    "experts": self.judge._serialized_experts(experts),
                    "confidence": 0,
                    "analysis": "",
                    "risks": [],
                    "action_plan": [],
                }
            else:
                decision = await self.judge.run(
                    experts=experts,
                    peer_review=peer,
                    fact_report=facts,
                    structural=structural,
                    consistency=consistency,
                    provider_errors=provider_errors,
                    category=category,
                )
                stages.append("judge")
            _mark("judge_ms", j0)
            await self._end(lifecycle, STEP_JUDGE, metadata={"execution_policy": policy})
        except Exception as exc:
            await self._fail(lifecycle, STEP_JUDGE, exc)
            raise

        try:
            await self._begin(lifecycle, STEP_FORMAT)
            f0 = time.monotonic()
            answer = await self.response_formatter.format(
                decision
            )
            await self.decision_memory.save(
                prompt,
                answer,
            )
            _mark("format_ms", f0)
            await self._end(lifecycle, STEP_FORMAT)
        except Exception as exc:
            await self._fail(lifecycle, STEP_FORMAT, exc)
            raise

        if policy == POLICY_LIGHTWEIGHT:
            text = ""
            if isinstance(answer, dict):
                text = str(answer.get("final_answer") or "").strip()
            if not text:
                raise RuntimeError("provider_generation_failed")

        latency["request_total_ms"] = int((time.monotonic() - t0) * 1000)
        self.last_stages_invoked = tuple(stages)
        self.last_latency_ms = dict(latency)
        return answer
