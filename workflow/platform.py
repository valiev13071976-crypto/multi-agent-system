"""Durable DAG workflow platform — multi-step execution with retry/timeout/HITL."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Awaitable, Callable, Mapping

from workflow.branching import resolve_branch
from workflow.compensation import CompensationHistory
from workflow.dag import ready_step_ids, validate_definition
from workflow.definition import (
    FAILURE_COMPENSATE,
    FAILURE_CONTINUE,
    FAILURE_FAIL_WORKFLOW,
    FAILURE_RETRY,
    FAILURE_SKIP,
    FAILURE_WAIT_FOR_HUMAN,
    STEP_TYPE_APPROVAL,
    STEP_TYPE_BRANCH,
    STEP_TYPE_SIDE_EFFECT,
    StepResult,
    WorkflowDefinition,
    WorkflowInstance,
    StepExecution,
)
from workflow.errors import (
    WorkflowDeadlineExceededError,
    WorkflowDefinitionError,
    WorkflowTimeoutError,
)
from workflow.models import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RETRY_WAIT,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_WAITING,
    TERMINAL_STATUSES,
    utc_now,
)
from workflow.registry import DefinitionRegistry
from workflow.retry import can_retry_attempt
from workflow.state_manager import StateManager


Handler = Callable[..., Awaitable[StepResult] | StepResult]


class WorkflowPlatform:
    """Deterministic multi-step DAG executor on top of StateManager."""

    def __init__(
        self,
        state_manager: StateManager,
        definitions: DefinitionRegistry | None = None,
        *,
        observability=None,
        autonomy_gate=None,
        hitl_service=None,
        side_effect_executor=None,
        compensation: CompensationHistory | None = None,
        workflow_engine=None,
    ):
        self.state_manager = state_manager
        self.definitions = definitions or DefinitionRegistry()
        self.observability = observability
        self.autonomy_gate = autonomy_gate
        self.hitl_service = hitl_service
        self.side_effect_executor = side_effect_executor
        self.compensation = compensation or CompensationHistory()
        self.workflow_engine = workflow_engine
        self._handlers: dict[str, Handler] = {}

    def register_handler(self, step_type: str, handler: Handler) -> None:
        self._handlers[str(step_type)] = handler

    def register_definition(self, definition: WorkflowDefinition) -> str:
        return self.definitions.register(definition)

    def _obs(self, event_type: str, workflow_id: str, **kwargs) -> None:
        if self.observability is None:
            return
        ctx = self.observability.context_for_workflow(workflow_id)
        if ctx is None:
            ctx = self.observability.create_context(workflow_id=workflow_id)
            if hasattr(self.observability, "bind_workflow_context"):
                self.observability.bind_workflow_context(workflow_id, ctx)
        self.observability.emit(
            event_type, context=ctx, component="workflow_platform", **kwargs
        )

    def create_instance(
        self,
        definition: WorkflowDefinition,
        *,
        task_id: str,
        execution_key: str | None = None,
        metadata=None,
        deadline_at=None,
        workflow_id: str | None = None,
        tenant_id: str | None = None,
    ):
        validate_definition(definition)
        timeout = definition.timeout_seconds
        deadline = deadline_at
        if deadline is None and timeout:
            deadline = utc_now() + timedelta(seconds=float(timeout))
        meta = {
            **dict(metadata or {}),
            "definition_key": definition.key,
        }
        if tenant_id:
            meta["tenant_id"] = tenant_id
        state = self.state_manager.create(
            task_id=task_id,
            workflow_id=workflow_id,
            execution_key=execution_key,
            step_names=definition.step_ids(),
            workflow_type=definition.workflow_type,
            definition_version=definition.version,
            metadata=meta,
            deadline_at=deadline,
            tenant_id=tenant_id,
        )
        self.state_manager.plan(state.workflow_id)
        self._obs(
            "workflow.created",
            state.workflow_id,
            status="created",
            metadata={
                "workflow_type": definition.workflow_type,
                "version": definition.version,
            },
        )
        return self.state_manager.get(state.workflow_id)

    def instance_view(self, workflow_id: str) -> WorkflowInstance:
        state = self.state_manager.get(workflow_id)
        definition = self._definition_for(state)
        skipped = frozenset(
            s.name for s in state.steps if s.status == STEP_SKIPPED
        )
        ready = ()
        if state.status not in TERMINAL_STATUSES and state.status != STATUS_WAITING_APPROVAL:
            if state.status != STATUS_RETRY_WAIT or (
                state.next_retry_at and state.next_retry_at <= utc_now()
            ):
                ready = ready_step_ids(definition, state, skipped=skipped)
        return WorkflowInstance(
            workflow_id=state.workflow_id,
            workflow_type=state.workflow_type or "",
            version=state.definition_version or "",
            status=state.status,
            current_steps=state.running_step_names(),
            ready_steps=ready,
            created_at=state.created_at,
            updated_at=state.updated_at,
            started_at=state.started_at,
            completed_at=state.completed_at,
            failed_at=state.failed_at,
            checkpoint_version=state.version,
            error_code=state.error_code,
            next_retry_at=state.next_retry_at,
            deadline_at=state.deadline_at,
            metadata=dict(state.metadata),
        )

    def status_payload(self, workflow_id: str) -> dict:
        view = self.instance_view(workflow_id)
        state = self.state_manager.get(workflow_id)
        steps = [
            {
                "step_id": s.name,
                "status": s.status,
                "attempt": s.attempt,
                "error_code": s.error_code,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            }
            for s in state.steps
        ]
        total = len(steps) or 1
        done = sum(1 for s in state.steps if s.status in {STEP_COMPLETED, STEP_SKIPPED})
        return {
            "workflow_id": view.workflow_id,
            "workflow_type": view.workflow_type,
            "version": view.version,
            "status": view.status,
            "ready_steps": list(view.ready_steps),
            "current_steps": list(view.current_steps),
            "waiting": view.status == STATUS_WAITING_APPROVAL,
            "progress": {"completed": done, "total": total},
            "steps": steps,
            "error_code": view.error_code,
            "next_retry_at": (
                view.next_retry_at.isoformat() if view.next_retry_at else None
            ),
            "deadline_at": view.deadline_at.isoformat() if view.deadline_at else None,
        }

    def _definition_for(self, state) -> WorkflowDefinition:
        key = (state.metadata or {}).get("definition_key")
        if key:
            return self.definitions.get_by_key(str(key))
        if state.workflow_type and state.definition_version:
            return self.definitions.get(state.workflow_type, state.definition_version)
        raise WorkflowDefinitionError(
            "definition_not_found",
            "Workflow has no definition reference",
        )

    def _step_results(self, state) -> dict[str, dict]:
        out = {}
        for record in state.steps:
            data = dict(record.metadata.get("result") or {})
            out[record.name] = data
        return out

    def recover_after_restart(self, workflow_id: str):
        state = self.state_manager.recover_interrupted_steps(workflow_id)
        self.state_manager.checkpoint(workflow_id)
        self._obs(
            "workflow.recovered",
            workflow_id,
            status=state.status,
            metadata={"recovered_interrupted_steps": True},
        )
        return state

    async def advance(self, workflow_id: str, *, max_steps: int = 100) -> dict:
        """Run ready steps until idle, blocked, or terminal."""

        executed = []
        for _ in range(max_steps):
            state = self.state_manager.get(workflow_id)
            if state.status in TERMINAL_STATUSES:
                break
            if state.status == STATUS_WAITING_APPROVAL:
                break
            if state.status == STATUS_RETRY_WAIT:
                if state.next_retry_at and state.next_retry_at > utc_now():
                    break
                self.state_manager.clear_retry_wait(workflow_id)
                if self.state_manager.get(workflow_id).status == STATUS_RETRY_WAIT:
                    self.state_manager.queue(workflow_id)
                state = self.state_manager.get(workflow_id)
            if state.deadline_at and utc_now() > state.deadline_at:
                await self._fail_deadline(workflow_id)
                break
            definition = self._definition_for(state)
            skipped = frozenset(
                s.name for s in state.steps if s.status == STEP_SKIPPED
            )
            ready = ready_step_ids(definition, state, skipped=skipped)
            if not ready:
                # all done?
                unfinished = [
                    s
                    for s in state.steps
                    if s.status not in {STEP_COMPLETED, STEP_SKIPPED}
                ]
                if not unfinished:
                    if state.status not in TERMINAL_STATUSES:
                        self.state_manager.complete_workflow(workflow_id)
                        self.state_manager.checkpoint(workflow_id)
                        self._obs(
                            "workflow.completed",
                            workflow_id,
                            status="completed",
                        )
                break
            # Ensure running
            state = self.state_manager.get(workflow_id)
            if state.status == "planned":
                self.state_manager.start(workflow_id)
            elif state.status == STATUS_QUEUED:
                self.state_manager.start(workflow_id)
            # Execute one wave of independent ready steps sequentially for determinism
            # (parallel-ready exists; we run them one-by-one to keep state transitions ordered)
            step_id = ready[0]
            outcome = await self._run_step(workflow_id, step_id)
            executed.append({"step_id": step_id, "outcome": outcome})
            if outcome in {"waiting_approval", "retry_wait", "failed", "cancelled"}:
                break
        return {
            "workflow_id": workflow_id,
            "status": self.state_manager.get(workflow_id).status,
            "executed": executed,
        }

    async def _fail_deadline(self, workflow_id: str) -> None:
        state = self.state_manager.get(workflow_id)
        current = state.current_step or (state.steps[0].name if state.steps else "workflow")
        self.state_manager.fail_step(workflow_id, current, "workflow_deadline_exceeded")
        self._obs(
            "workflow.timeout",
            workflow_id,
            status="failed",
            error_code="workflow_deadline_exceeded",
            metadata={"scope": "workflow"},
        )

    async def _run_step(self, workflow_id: str, step_id: str) -> str:
        state = self.state_manager.get(workflow_id)
        definition = self._definition_for(state)
        step = definition.step(step_id)
        if step is None:
            raise WorkflowDefinitionError(
                "unknown_step", f"Step not in definition: {step_id}"
            )
        record = state.step(step_id)
        if record and record.status == STEP_COMPLETED:
            return "already_completed"

        if step.requires_approval or step.step_type == STEP_TYPE_APPROVAL:
            cleared = bool(record and record.metadata.get("approval_cleared"))
            if not cleared:
                if record and record.status != STEP_WAITING:
                    if state.status != STATUS_WAITING_APPROVAL:
                        self.state_manager.start_step(workflow_id, step_id)
                        self.state_manager.wait_for_approval(workflow_id)
                        self.state_manager.checkpoint(
                            workflow_id,
                            extra_payload={"waiting_step": step_id},
                        )
                        self._obs(
                            "workflow.waiting_approval",
                            workflow_id,
                            status="waiting_approval",
                            metadata={"step_id": step_id},
                        )
                        return "waiting_approval"
                if state.status == STATUS_WAITING_APPROVAL:
                    return "waiting_approval"

        started, ok = self.state_manager.start_step(workflow_id, step_id)
        if not ok:
            return "skipped_start"
        self._obs(
            "workflow.step.started",
            workflow_id,
            status="running",
            metadata={"step_id": step_id, "attempt": started.step(step_id).attempt},
        )
        mono0 = (
            self.observability.monotonic_ms() if self.observability is not None else None
        )
        try:
            result = await self._invoke_step(workflow_id, step, started)
        except WorkflowTimeoutError as exc:
            return await self._handle_failure(
                workflow_id, step, error_code=exc.error_code, error_class=type(exc).__name__
            )
        except Exception as exc:
            code = getattr(exc, "error_code", None) or type(exc).__name__
            return await self._handle_failure(
                workflow_id,
                step,
                error_code=str(code),
                error_class=type(exc).__name__,
            )

        if not result.ok:
            return await self._handle_failure(
                workflow_id,
                step,
                error_code=result.error_code or "step_failed",
                error_class=result.error_class,
            )

        result_data = dict(result.data or {})
        # Bounded multi-slice step: persist progress and re-run until continue_step clears
        if result_data.get("continue_step"):
            progress = dict(result_data)
            progress.pop("continue_step", None)
            meta = {
                "result": progress,
                "result_ref": result.result_ref,
                "progress": progress,
            }
            self.state_manager.defer_step(workflow_id, step_id, metadata=meta)
            self.state_manager.checkpoint(workflow_id)
            self._obs(
                "workflow.step.continued",
                workflow_id,
                status="running",
                metadata={
                    "step_id": step_id,
                    "batch_index": progress.get("batch_index"),
                    "batches_remaining": progress.get("batches_remaining"),
                },
            )
            return "continue_step"

        meta = {
            "result": result_data,
            "result_ref": result.result_ref,
        }
        self.state_manager.complete_step(workflow_id, step_id, metadata=meta)
        duration = None
        if mono0 is not None and self.observability is not None:
            duration = int(self.observability.monotonic_ms() - mono0)
        self._obs(
            "workflow.step.completed",
            workflow_id,
            status="completed",
            duration_ms=duration,
            metadata={"step_id": step_id},
        )

        # Branch resolution
        if step.branch is not None or step.step_type == STEP_TYPE_BRANCH:
            rule = step.branch
            if rule is not None:
                state = self.state_manager.get(workflow_id)
                activate, skip = resolve_branch(rule, self._step_results(state))
                for sid in skip:
                    self.state_manager.skip_step(
                        workflow_id, sid, reason="branch_not_taken"
                    )
                    self._obs(
                        "workflow.step.skipped",
                        workflow_id,
                        status="skipped",
                        metadata={"step_id": sid, "reason": "branch_not_taken"},
                    )
                # activate steps stay pending with deps possibly including this step
                _ = activate

        self.state_manager.checkpoint(workflow_id)
        return "completed"

    async def _invoke_step(self, workflow_id, step, state) -> StepResult:
        timeout = step.timeout_seconds
        if state.deadline_at:
            remaining = (state.deadline_at - utc_now()).total_seconds()
            if remaining <= 0:
                raise WorkflowDeadlineExceededError()
            if timeout is None:
                timeout = remaining
            else:
                timeout = min(float(timeout), float(remaining))

        async def _call() -> StepResult:
            if step.step_type == STEP_TYPE_BRANCH and step.step_id not in self._handlers:
                # pure branch marker — success with empty data; results come from deps
                return StepResult(ok=True, data={})
            handler = self._handlers.get(step.step_id) or self._handlers.get(step.step_type)
            if handler is None:
                raise WorkflowDefinitionError(
                    "handler_missing",
                    f"No handler for step_type={step.step_type!r}",
                    step_id=step.step_id,
                )
            ctx = {
                "workflow_id": workflow_id,
                "step": step,
                "state": state,
                "platform": self,
            }
            pending = handler(ctx)
            if asyncio.iscoroutine(pending):
                pending = await pending
            if isinstance(pending, StepResult):
                return pending
            if isinstance(pending, Mapping):
                return StepResult(ok=True, data=dict(pending))
            return StepResult(ok=True, data={"value": pending})

        if timeout is None:
            return await _call()
        try:
            return await asyncio.wait_for(_call(), timeout=float(timeout))
        except asyncio.TimeoutError as exc:
            self._obs(
                "workflow.step.timeout",
                workflow_id,
                status="failed",
                error_code="step_timeout",
                metadata={"step_id": step.step_id, "scope": "step"},
            )
            raise WorkflowTimeoutError("step_timeout", scope="step") from exc

    async def _handle_failure(
        self, workflow_id: str, step, *, error_code: str, error_class: str | None
    ) -> str:
        state = self.state_manager.get(workflow_id)
        record = state.step(step.step_id)
        attempt = int(record.attempt) if record else 1
        policy = step.failure_policy
        self._obs(
            "workflow.step.failed",
            workflow_id,
            status="failed",
            error_code=error_code,
            metadata={
                "step_id": step.step_id,
                "attempt": attempt,
                "failure_policy": policy,
                "error_class": error_class,
            },
        )

        if policy == FAILURE_RETRY or (
            policy == FAILURE_FAIL_WORKFLOW
            and can_retry_attempt(
                attempt, error_code, step.retry_policy, error_class=error_class
            )
        ):
            if can_retry_attempt(
                attempt, error_code, step.retry_policy, error_class=error_class
            ):
                delay = step.retry_policy.delay_seconds(attempt)
                next_at = utc_now() + timedelta(seconds=delay)
                self.state_manager.mark_step_failed(workflow_id, step.step_id, error_code)
                self.state_manager.bump_step_attempt(workflow_id, step.step_id)
                self.state_manager.mark_retry_wait(
                    workflow_id, next_retry_at=next_at, error_code=error_code
                )
                self.state_manager.checkpoint(workflow_id)
                self._obs(
                    "workflow.retry_scheduled",
                    workflow_id,
                    status="retry_wait",
                    metadata={
                        "step_id": step.step_id,
                        "next_retry_at": next_at.isoformat(),
                        "attempt": attempt + 1,
                    },
                )
                return "retry_wait"
            # exhausted
            policy = FAILURE_FAIL_WORKFLOW

        if policy == FAILURE_SKIP or policy == FAILURE_CONTINUE:
            self.state_manager.mark_step_failed(workflow_id, step.step_id, error_code)
            # mark as skipped-for-continue semantics: convert failed → skipped continue
            if policy == FAILURE_SKIP:
                # leave as failed then skip overlay — use skip for DAG readiness
                self.state_manager.skip_step(
                    workflow_id, step.step_id, reason=f"failure_policy_skip:{error_code}"
                )
            else:
                # continue: treat as skipped so dependents may proceed
                self.state_manager.skip_step(
                    workflow_id,
                    step.step_id,
                    reason=f"failure_policy_continue:{error_code}",
                )
            self.state_manager.checkpoint(workflow_id)
            return "continued"

        if policy == FAILURE_WAIT_FOR_HUMAN:
            self.state_manager.mark_step_failed(workflow_id, step.step_id, error_code)
            # reset to waiting
            state = self.state_manager.get(workflow_id)
            # put step back to waiting
            from dataclasses import replace

            rec = state.step(step.step_id)
            if rec:
                now = utc_now()
                rec = replace(rec, status=STEP_WAITING, error_code=error_code)
                steps = tuple(
                    rec if s.name == step.step_id else s for s in state.steps
                )
                state = replace(
                    state,
                    steps=steps,
                    updated_at=now,
                    version=state.version + 1,
                )
                self.state_manager._store.save(state)
            if self.state_manager.get(workflow_id).status != STATUS_WAITING_APPROVAL:
                self.state_manager.wait_for_approval(workflow_id)
            self.state_manager.checkpoint(workflow_id)
            return "waiting_approval"

        if policy == FAILURE_COMPENSATE:
            await self._compensate_committed(workflow_id, step)
            self.state_manager.fail_step(workflow_id, step.step_id, error_code)
            self._obs(
                "workflow.compensation",
                workflow_id,
                status="failed",
                metadata={"step_id": step.step_id},
            )
            return "failed"

        # fail_workflow
        self.state_manager.fail_step(workflow_id, step.step_id, error_code)
        self.state_manager.checkpoint(workflow_id)
        return "failed"

    async def _compensate_committed(self, workflow_id: str, step) -> None:
        state = self.state_manager.get(workflow_id)
        # Compensate prior completed side_effect steps that declared compensation_action
        definition = self._definition_for(state)
        for prior in definition.steps:
            if prior.step_id == step.step_id:
                break
            rec = state.step(prior.step_id)
            if rec is None or rec.status != STEP_COMPLETED:
                continue
            if prior.step_type != STEP_TYPE_SIDE_EFFECT:
                continue
            if not prior.compensation_action:
                continue
            execution_id = str(rec.metadata.get("result_ref") or "")
            if not execution_id:
                continue
            try:
                engine = self.workflow_engine
                if engine is None:
                    raise RuntimeError("workflow_engine_required_for_compensation")
                from autonomy.gate import build_proposed_action

                action = build_proposed_action(
                    action_type=prior.compensation_action,
                    workflow_id=workflow_id,
                    task_id=state.task_id,
                    resource=execution_id,
                    idempotency_key=f"compensate:{workflow_id}:{prior.step_id}:{execution_id}",
                    metadata={"compensation": True, "step_id": prior.step_id},
                    action_id=f"compensate:{workflow_id}:{prior.step_id}",
                )
                await engine.rollback_side_effect(execution_id, action)
                self.compensation.record_success(
                    workflow_id=workflow_id,
                    step_id=prior.step_id,
                    execution_id=execution_id,
                )
                hist = list(state.metadata.get("compensation_history") or [])
                hist.append(
                    {
                        "step_id": prior.step_id,
                        "execution_id": execution_id,
                        "status": "compensated",
                    }
                )
                self.state_manager.set_metadata(
                    workflow_id, {"compensation_history": hist}
                )
            except Exception as exc:
                code = getattr(exc, "error_code", None) or type(exc).__name__
                self.compensation.record_failure(
                    workflow_id=workflow_id,
                    step_id=prior.step_id,
                    execution_id=execution_id,
                    error_code=str(code),
                )
                hist = list(
                    self.state_manager.get(workflow_id).metadata.get(
                        "compensation_history"
                    )
                    or []
                )
                hist.append(
                    {
                        "step_id": prior.step_id,
                        "execution_id": execution_id,
                        "status": "compensation_failed",
                        "error_code": str(code),
                    }
                )
                self.state_manager.set_metadata(
                    workflow_id, {"compensation_history": hist}
                )
                self._obs(
                    "workflow.compensation_failed",
                    workflow_id,
                    status="failed",
                    error_code=str(code),
                    metadata={"step_id": prior.step_id},
                )

    def cancel(self, workflow_id: str) -> dict:
        state = self.state_manager.get(workflow_id)
        if state.status in TERMINAL_STATUSES:
            return {"cancelled": False, "status": state.status, "reason": "terminal"}
        self.state_manager.cancel(workflow_id)
        self.state_manager.checkpoint(workflow_id)
        self._obs("workflow.cancelled", workflow_id, status="cancelled")
        return {"cancelled": True, "status": STATUS_CANCELLED}

    def step_execution(self, workflow_id: str, step_id: str) -> StepExecution | None:
        state = self.state_manager.get(workflow_id)
        record = state.step(step_id)
        if record is None:
            return None
        return StepExecution(
            workflow_id=workflow_id,
            step_id=step_id,
            attempt=record.attempt,
            status=record.status,
            started_at=record.started_at,
            completed_at=record.completed_at,
            result_ref=record.metadata.get("result_ref"),
            error_code=record.error_code,
            error_class=record.metadata.get("error_class"),
            metadata=dict(record.metadata),
        )
