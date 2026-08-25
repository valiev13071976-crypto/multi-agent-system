import asyncio
from dataclasses import dataclass

from autonomy.errors import AutonomyDeniedError
from autonomy.gate import queue_side_effect_permitted
from hitl.errors import (
    ExecutionPermitConsumedError,
    ExecutionPermitExpiredError,
    ExecutionPermitMismatchError,
    ExecutionPermitRevokedError,
)
from hitl.permit import PermitService
from task_queue.errors import QueueTimeoutError
from task_queue.models import STATUS_CANCELLED, QueueTask
from task_queue.queue import TaskQueue
from workflow.engine import WorkflowEngine
from workflow.models import STATUS_FAILED


# Timeout stops waiting on the handler coroutine. It does not guarantee that an
# in-flight provider HTTP call is aborted by the remote server.
TIMEOUT_CANCELS_WAIT_ONLY = True

# TaskQueue concurrency is execution-unit scoped. mode=both may still gather
# providers inside one already-started execution. That is not a queue fix.
MODE_BOTH_BUDGET_LIMITATION = (
    "mode=both may start concurrent provider calls inside one execution; "
    "TaskQueue does not serialize those calls."
)


@dataclass(frozen=True)
class WorkerConfig:
    max_concurrency: int = 1


@dataclass(frozen=True)
class ExecutionContext:
    queue_task_id: str
    workflow_id: str
    task_id: str
    execution_key: str
    attempt: int
    lease_id: str


class ExecutionContextRegistry:
    """Ephemeral in-memory handler registry. Never persisted."""

    def __init__(self):
        self._handlers: dict[str, object] = {}

    def register(self, execution_key: str, handler) -> None:
        self._handlers[execution_key] = handler

    def get(self, execution_key: str):
        return self._handlers.get(execution_key)

    def drop(self, execution_key: str) -> None:
        self._handlers.pop(execution_key, None)


class TaskWorker:
    def __init__(
        self,
        queue: TaskQueue,
        handler=None,
        *,
        engine: WorkflowEngine | None = None,
        registry: ExecutionContextRegistry | None = None,
        config: WorkerConfig | None = None,
    ):
        self.queue = queue
        self.handler = handler
        self.engine = engine
        self.registry = registry or ExecutionContextRegistry()
        self.config = config or WorkerConfig()

    def require_autonomy_allow(self, decision) -> None:
        if not queue_side_effect_permitted(decision):
            raise AutonomyDeniedError("autonomy_not_allow")

    async def execute_side_effect(self, action, **kwargs):
        """Future queue tasks must invoke SideEffectExecutor, never an adapter."""
        if self.engine is None:
            from side_effects.errors import SideEffectExecutionDeniedError

            raise SideEffectExecutionDeniedError("workflow_engine_required")
        return await self.engine.execute_side_effect(action, **kwargs)

    def require_execution_permit(self, permit, action=None, *, now=None) -> None:
        if permit is None:
            raise AutonomyDeniedError("execution_permit_required")
        try:
            PermitService().validate(permit, action=action, now=now)
        except (
            ExecutionPermitExpiredError,
            ExecutionPermitConsumedError,
            ExecutionPermitRevokedError,
            ExecutionPermitMismatchError,
        ) as exc:
            raise AutonomyDeniedError(str(exc)) from exc

    async def run_once(self) -> QueueTask | None:
        task = self.queue.dequeue()
        if task is None:
            return None
        return await self.execute(task)

    async def execute(self, task: QueueTask) -> QueueTask:
        lease_id = task.lease_id
        if lease_id is None:
            raise RuntimeError("dequeued task missing lease_id")
        if self.engine is not None:
            gate = self.engine.queue_execution_gate(task.workflow_id)
            if gate == "waiting_approval":
                return self.queue.defer_waiting_approval(task.queue_task_id, lease_id)
            if gate == "cancelled":
                return self.queue.cancel(task.queue_task_id)
            if gate == "completed":
                return self.queue.skip_complete(
                    task.queue_task_id,
                    lease_id,
                    reason="workflow_completed",
                )
            if gate == "failed":
                return self.queue.cancel(task.queue_task_id)
        current = self.queue.get(task.queue_task_id)
        if current.metadata.get("cancellation_requested"):
            return self.queue.cancel(task.queue_task_id)
        running = self.queue.start(task.queue_task_id, lease_id)
        if running.metadata.get("cancellation_requested"):
            return self.queue.abort_running(task.queue_task_id, lease_id)
        handler = self.registry.get(running.execution_key) or self.handler
        ctx = ExecutionContext(
            queue_task_id=running.queue_task_id,
            workflow_id=running.workflow_id,
            task_id=running.task_id,
            execution_key=running.execution_key,
            attempt=running.attempt,
            lease_id=lease_id,
        )
        try:
            if handler is None:
                raise RuntimeError("no_queue_handler")
            await self._invoke(handler, ctx, running.timeout_seconds)
        except QueueTimeoutError:
            if self.engine is not None:
                self._fail_workflow(running.workflow_id, "execution_timeout")
            return self.queue.fail(
                running.queue_task_id,
                lease_id,
                error_code="execution_timeout",
            )
        except Exception as exc:
            code = type(exc).__name__
            message = str(exc)
            if hasattr(exc, "error_code"):
                code = str(getattr(exc, "error_code") or code)
            if self.engine is not None:
                self._fail_workflow(running.workflow_id, code)
            return self.queue.fail(
                running.queue_task_id,
                lease_id,
                error_code=code,
                metadata={"error_class": type(exc).__name__, "error_message": message},
            )
        return self.queue.ack(running.queue_task_id, lease_id)

    async def _invoke(self, handler, ctx: ExecutionContext, timeout_seconds: float | None):
        pending = handler(ctx)
        if not asyncio.iscoroutine(pending):
            return pending
        if timeout_seconds is None:
            return await pending
        try:
            return await asyncio.wait_for(pending, timeout=float(timeout_seconds))
        except asyncio.TimeoutError as exc:
            raise QueueTimeoutError() from exc

    def _fail_workflow(self, workflow_id: str, error_code: str) -> None:
        try:
            state = self.engine.state_manager.get(workflow_id)
        except Exception:
            return
        if state.status in {STATUS_CANCELLED, STATUS_FAILED}:
            return
        if state.status == "completed":
            return
        current = state.current_step or "route"
        try:
            self.engine.state_manager.fail_step(workflow_id, current, error_code)
        except Exception:
            return
