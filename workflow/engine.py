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
    "ProviderCapabilityMismatchError": "provider_capability_mismatch",
    "BudgetRoutingDeniedError": "finops_budget_denied",
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
        hitl_service=None,
        side_effect_executor=None,
        reconciliation_service=None,
        observability=None,
        memory_service=None,
        document_service=None,
        knowledge_service=None,
        procurement_service=None,
    ):
        self.state_manager = state_manager or StateManager(step_names=step_names)
        self.protected_steps = protected_steps
        self.autonomy_gate = autonomy_gate
        self.hitl_service = hitl_service
        self.side_effect_executor = side_effect_executor
        self.reconciliation_service = reconciliation_service
        self.observability = observability
        self.memory_service = memory_service
        self.document_service = document_service
        self.knowledge_service = knowledge_service
        self.procurement_service = procurement_service
        self.last_workflow_id = None
        self.last_task_id = None
        self.last_approval_id = None
        self.last_permit_id = None

    def retrieve_memory_context(self, query, *, requesting_scope=None):
        """Optional DI: request scoped memory via MemoryService (no auto-write)."""
        if self.memory_service is None:
            return ()
        results = self.memory_service.retrieve(query, requesting_scope=requesting_scope)
        return self.memory_service.build_context(results)

    def ingest_document(self, request, *, requesting_scope=None):
        """Optional DI: scoped document ingest via DocumentService."""
        if self.document_service is None:
            raise RuntimeError("document_service_unavailable")
        return self.document_service.ingest(request, requesting_scope=requesting_scope)

    def retrieve_knowledge_context(self, query, *, requesting_scope=None):
        """Optional DI: hybrid knowledge/RAG context via KnowledgeService."""
        if self.knowledge_service is None:
            return None
        return self.knowledge_service.retrieve_knowledge_context(
            query, requesting_scope=requesting_scope
        )

    def run_procurement(self, request_id: str, *, requesting_scope, **kwargs):
        """Optional DI: run procurement workflow via ProcurementService."""
        if self.procurement_service is None:
            raise RuntimeError("procurement_service_unavailable")
        from procurement.workflow import ProcurementWorkflow

        return ProcurementWorkflow(self.procurement_service).run(
            request_id, requesting_scope=requesting_scope, **kwargs
        )

    def _obs_ctx(self, workflow_id: str = "", task_id: str = ""):
        obs = self.observability
        if obs is None:
            return None
        if workflow_id:
            existing = obs.context_for_workflow(workflow_id)
            if existing is not None:
                return existing
        return obs.create_context(workflow_id=workflow_id, task_id=task_id)

    def _obs_emit(self, event_type: str, ctx, **kwargs) -> None:
        if self.observability is None:
            return
        self.observability.emit(
            event_type, context=ctx, component="workflow", **kwargs
        )

    def create(self, task_id: str, *, tenant_id: str | None = None) -> str:
        state = self.state_manager.create(task_id=task_id, tenant_id=tenant_id)
        self.last_workflow_id = state.workflow_id
        self.last_task_id = task_id
        ctx = self._obs_ctx(state.workflow_id, task_id)
        if self.observability is not None and ctx is not None:
            self.observability.bind_workflow_context(state.workflow_id, ctx)
        self._obs_emit("workflow.created", ctx, status="created")
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
        if state.status == "retry_wait":
            if state.next_retry_at is not None:
                from workflow.models import utc_now

                if state.next_retry_at > utc_now():
                    return "waiting_approval"  # defer until retry window (reuse defer)
            return "execute"
        return "execute"

    def _gate(self) -> AutonomyGate:
        if self.autonomy_gate is None:
            self.autonomy_gate = AutonomyGate()
        return self.autonomy_gate

    def _hitl(self):
        from hitl.service import HITLService

        if self.hitl_service is None:
            gate = self._gate()
            self.hitl_service = HITLService(
                gate=gate,
                state_manager=self.state_manager,
                store=gate.approvals.store,
            )
        return self.hitl_service

    def evaluate_action(self, action, **kwargs):
        """Policy lives in AutonomyGate. HITL owns approval lifecycle."""
        requested_by = kwargs.pop("requested_by", "system")
        now = kwargs.get("now")
        gate = self._gate()
        if self.observability is not None and getattr(gate, "observability", None) is None:
            gate.observability = self.observability
            gate.obs_context = self._obs_ctx(action.workflow_id, action.task_id)
        decision = gate.evaluate(action, **kwargs)
        if decision.decision == DECISION_REQUIRE_APPROVAL:
            hitl = self._hitl()
            if self.observability is not None and getattr(hitl, "observability", None) is None:
                hitl.observability = self.observability
            record = hitl.request_approval(
                action,
                decision,
                requested_by=requested_by,
                now=now,
            )
            self.last_approval_id = record.approval_id
            ctx = self._obs_ctx(action.workflow_id, action.task_id)
            self._obs_emit(
                "workflow.waiting_approval",
                ctx,
                status="waiting_approval",
                metadata={"approval_id": record.approval_id},
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
        tenant_id: str | None = None,
    ):
        workflow_id = self.create(task_id, tenant_id=tenant_id)
        manager = self.state_manager
        manager.plan(workflow_id)
        manager.start(workflow_id)
        ctx = self._obs_ctx(workflow_id, task_id)
        mono0 = (
            self.observability.monotonic_ms() if self.observability is not None else None
        )
        self._obs_emit("workflow.started", ctx, status="started")
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
            duration = None
            if mono0 is not None and self.observability is not None:
                duration = int(self.observability.monotonic_ms() - mono0)
            self._obs_emit(
                "workflow.completed",
                ctx,
                status="completed",
                duration_ms=duration,
            )
            return result
        except Exception as exc:
            state = manager.get(workflow_id)
            if state.status not in TERMINAL_STATUSES:
                current = state.current_step or "route"
                manager.fail_step(workflow_id, current, error_code_for(exc))
            self._obs_emit(
                "workflow.failed",
                ctx,
                status="failed",
                error_code=error_code_for(exc),
                exception_type=type(exc).__name__,
            )
            raise

    async def resume(
        self,
        workflow_id: str,
        handlers: dict | None = None,
        *,
        execution_permit=None,
        action=None,
    ):
        state = self.state_manager.get(workflow_id)
        if state.status in TERMINAL_STATUSES:
            return {"ran": False, "reason": "terminal", "status": state.status}
        if execution_permit is not None:
            self._hitl().permits.validate(
                execution_permit,
                action=action,
            )
            current = self.state_manager.get(workflow_id)
            if current.status == STATUS_WAITING_APPROVAL:
                self.state_manager.approve(workflow_id)
            self.last_permit_id = execution_permit.permit_id
            ctx = self._obs_ctx(workflow_id)
            self._obs_emit(
                "workflow.resumed",
                ctx,
                status="resumed",
                metadata={"permit_id": execution_permit.permit_id},
            )
            return {
                "ran": False,
                "reason": "ready_for_execution",
                "ready_for_execution": True,
                "permit_id": execution_permit.permit_id,
                "status": self.state_manager.get(workflow_id).status,
            }
        if state.status == STATUS_WAITING_APPROVAL:
            return {
                "ran": False,
                "reason": "waiting_approval",
                "ready_for_execution": False,
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
                self._obs_emit(
                    "workflow.completed",
                    self._obs_ctx(workflow_id),
                    status="completed",
                )
        return {
            "ran": True,
            "reason": "resumed",
            "executed": executed,
            "status": self.state_manager.get(workflow_id).status,
        }

    async def execute_side_effect(self, action, **kwargs):
        """Delegate side-effect invocation. Policy stays in AutonomyGate/HITL."""
        from side_effects.executor import SideEffectExecutor

        executor = self.side_effect_executor or SideEffectExecutor()
        gate = kwargs.pop("gate", None) or self._gate()
        hitl = kwargs.pop("hitl", None)
        if kwargs.get("permit") is not None and hitl is None:
            hitl = self._hitl()
        return await executor.execute(
            action,
            gate=gate,
            hitl=hitl,
            state_manager=kwargs.pop("state_manager", self.state_manager),
            **kwargs,
        )

    async def rollback_side_effect(self, execution_id: str, action, **kwargs):
        from side_effects.executor import SideEffectExecutor

        executor = self.side_effect_executor or SideEffectExecutor()
        gate = kwargs.pop("gate", None) or self._gate()
        hitl = kwargs.pop("hitl", None)
        if kwargs.get("permit") is not None and hitl is None:
            hitl = self._hitl()
        return await executor.rollback(
            execution_id,
            action=action,
            gate=gate,
            hitl=hitl,
            **kwargs,
        )

    async def reconcile_side_effect(self, reconciliation_id: str, **kwargs):
        service = self.reconciliation_service
        if service is None:
            from side_effects.errors import ReconciliationNotEligibleError

            raise ReconciliationNotEligibleError("reconciliation_service_not_configured")
        return await service.reconcile(reconciliation_id, **kwargs)
