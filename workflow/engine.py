from autonomy.gate import AutonomyGate
from autonomy.models import DECISION_REQUIRE_APPROVAL
from workflow.errors import WaitingApprovalError, WorkflowNotFoundError
from workflow.models import (
    ANALYZE_STEPS,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_WAITING_APPROVAL,
    STEP_VALIDATE,
    TERMINAL_STATUSES,
)
from workflow.state_manager import StateManager


ERROR_CODES = {
    "InvalidModeError": "invalid_mode",
    "InvalidRoleError": "invalid_role",
    "ProviderNotConfiguredError": "provider_not_configured",
    "NoProvidersAvailableError": "no_providers_available",
    "NoCapableProviderError": "no_capable_provider",
    "FinOpsBudgetDeniedError": "finops_budget_denied",
}


def error_code_for(exc: BaseException) -> str:
    return ERROR_CODES.get(type(exc).__name__, type(exc).__name__)


class WorkflowLifecycle:
    def __init__(
        self,
        manager: StateManager,
        workflow_id: str,
        protected_steps: frozenset[str] = frozenset(),
    ):
        self.manager = manager
        self.workflow_id = workflow_id
        self.protected_steps = protected_steps

    async def begin(self, name: str) -> bool:
        state = self.manager.get(self.workflow_id)
        if state.status == STATUS_WAITING_APPROVAL and name in self.protected_steps:
            raise WaitingApprovalError()
        if name == STEP_VALIDATE:
            self.manager.mark_validating(self.workflow_id)
        _state, started = self.manager.start_step(self.workflow_id, name)
        return started

    async def end(self, name: str, metadata=None) -> None:
        self.manager.complete_step(self.workflow_id, name, metadata=metadata)
        if name == STEP_VALIDATE:
            self.manager.mark_running(self.workflow_id)
        self.manager.checkpoint(self.workflow_id)

    async def fail(self, name: str, error_code: str) -> None:
        self.manager.fail_step(self.workflow_id, name, error_code)


class WorkflowEngine:
    def __init__(
        self,
        state_manager: StateManager | None = None,
        protected_steps: frozenset[str] = frozenset(),
        step_names=ANALYZE_STEPS,
        autonomy_gate: AutonomyGate | None = None,
    ):
        self.state_manager = state_manager or StateManager(step_names=step_names)
        self.protected_steps = protected_steps
        self.autonomy_gate = autonomy_gate
        self.last_workflow_id = None
        self.last_task_id = None
        self.last_approval_id = None

    def create(self, task_id: str) -> str:
        state = self.state_manager.create(task_id=task_id)
        self.last_workflow_id = state.workflow_id
        self.last_task_id = task_id
        return state.workflow_id

    def queue_execution_gate(self, workflow_id: str) -> str:
        """Machine-safe worker gate. Does not mutate workflow state."""
        try:
            state = self.state_manager.get(workflow_id)
        except WorkflowNotFoundError:
            return "missing"
        if state.status == STATUS_WAITING_APPROVAL:
            return "waiting_approval"
        if state.status == STATUS_CANCELLED:
            return "cancelled"
        if state.status == STATUS_COMPLETED:
            return "completed"
        if state.status == STATUS_FAILED:
            return "failed"
        return "execute"

    def _gate(self) -> AutonomyGate:
        if self.autonomy_gate is None:
            self.autonomy_gate = AutonomyGate()
        return self.autonomy_gate

    def evaluate_action(self, action, **kwargs):
        """Policy lives in AutonomyGate. Engine only applies workflow effects."""
        gate = self._gate()
        decision = gate.evaluate(action, **kwargs)
        if decision.decision == DECISION_REQUIRE_APPROVAL:
            state = self.state_manager.get(action.workflow_id)
            if state.status != STATUS_WAITING_APPROVAL:
                self.state_manager.wait_for_approval(action.workflow_id)
            record = gate.approvals.create_pending(
                workflow_id=action.workflow_id,
                task_id=action.task_id,
                action_id=action.action_id,
                decision_id=decision.decision_id,
            )
            self.last_approval_id = record.approval_id
            self.state_manager.checkpoint(
                action.workflow_id,
                extra_payload={
                    "action_id": action.action_id,
                    "decision_id": decision.decision_id,
                    "required_approval": True,
                    "approval_id": record.approval_id,
                },
            )
        return decision

    def resolve_action_approval(
        self,
        approval_id: str,
        status: str,
        *,
        approved_by: str,
        action,
        reason_code: str | None = None,
        **evaluate_kwargs,
    ):
        gate = self._gate()
        record = gate.approvals.resolve(
            approval_id,
            status,
            approved_by=approved_by,
            reason_code=reason_code,
        )
        state = self.state_manager.get(action.workflow_id)
        if status == "approved" and state.status == STATUS_WAITING_APPROVAL:
            self.state_manager.approve(action.workflow_id)
        return gate.evaluate(action, approval=record, **evaluate_kwargs)

    async def execute(
        self,
        prompt: str,
        mode: str | None,
        role: str | None,
        *,
        context_manager,
        run_router,
        task_id: str,
    ):
        workflow_id = self.create(task_id)
        manager = self.state_manager
        manager.plan(workflow_id)
        manager.start(workflow_id)
        lifecycle = WorkflowLifecycle(
            manager,
            workflow_id,
            protected_steps=self.protected_steps,
        )
        try:
            await lifecycle.begin(ANALYZE_STEPS[0])
            prepared = await context_manager.prepare(prompt)
            await lifecycle.end(
                ANALYZE_STEPS[0],
                metadata={"prepared_chars": len(str(prepared))},
            )
            result = await run_router(
                prompt=str(prepared),
                mode=mode,
                role=role,
                task_id=task_id,
                lifecycle=lifecycle,
            )
            manager.complete_workflow(workflow_id)
            manager.checkpoint(workflow_id)
            return result
        except Exception as exc:
            state = manager.get(workflow_id)
            if state.status not in TERMINAL_STATUSES:
                current = state.current_step or "route"
                manager.fail_step(workflow_id, current, error_code_for(exc))
            raise

    async def resume(self, workflow_id: str, handlers: dict | None = None):
        state = self.state_manager.get(workflow_id)
        if state.status in TERMINAL_STATUSES:
            return {"ran": False, "reason": "terminal", "status": state.status}
        if state.status == STATUS_WAITING_APPROVAL:
            return {
                "ran": False,
                "reason": "waiting_approval",
                "status": state.status,
            }
        handlers = handlers or {}
        lifecycle = WorkflowLifecycle(
            self.state_manager,
            workflow_id,
            protected_steps=self.protected_steps,
        )
        executed = []
        for name in self.state_manager._step_names:
            record = state.step(name)
            state = self.state_manager.get(workflow_id)
            if state.status == STATUS_WAITING_APPROVAL and name in self.protected_steps:
                return {
                    "ran": True,
                    "reason": "waiting_approval",
                    "executed": executed,
                    "status": state.status,
                }
            if record and record.status == "completed":
                continue
            handler = handlers.get(name)
            if handler is None:
                continue
            started = await lifecycle.begin(name)
            if not started:
                continue
            await handler()
            await lifecycle.end(name)
            executed.append(name)
            state = self.state_manager.get(workflow_id)
        if self.state_manager.get(workflow_id).next_incomplete_step() is None:
            if self.state_manager.get(workflow_id).status not in TERMINAL_STATUSES:
                self.state_manager.complete_workflow(workflow_id)
        return {
            "ran": True,
            "reason": "resumed",
            "executed": executed,
            "status": self.state_manager.get(workflow_id).status,
        }
