import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from autonomy.errors import AutonomyDeniedError
from autonomy.gate import queue_side_effect_permitted
from task_queue.errors import QueueLeaseError, QueueTimeoutError
from task_queue.models import STATUS_CANCELLED, QueueTask
from task_queue.pools import POOL_NORMAL, PoolConfig
from task_queue.queue import TaskQueue

if TYPE_CHECKING:
    from workflow.engine import WorkflowEngine


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
    allowed_lanes: frozenset[str] | None = None
    pool_name: str = POOL_NORMAL
    draining: bool = False

    @classmethod
    def from_env(cls, env=None, *, pool_name: str | None = None) -> "WorkerConfig":
        pool = PoolConfig.from_env(env, pool_name=pool_name)
        return cls(
            max_concurrency=pool.max_concurrency,
            allowed_lanes=pool.allowed_lanes,
            pool_name=pool.name,
            draining=False,
        )


@dataclass(frozen=True)
class ExecutionContext:
    queue_task_id: str
    workflow_id: str
    task_id: str
    execution_key: str
    attempt: int
    lease_id: str
    tenant_id: str = ""
    user_id: str = ""
    actor_ref: str = ""
    worker_id: str = ""


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
        engine: "WorkflowEngine | None" = None,
        registry: ExecutionContextRegistry | None = None,
        config: WorkerConfig | None = None,
        worker_id: str | None = None,
    ):
        self.queue = queue
        self.handler = handler
        self.engine = engine
        self.registry = registry or ExecutionContextRegistry()
        self.config = config or WorkerConfig()
        self.worker_id = str(worker_id or f"worker-{uuid.uuid4().hex[:8]}")
        self._draining = bool(self.config.draining)
        # Bind claim filter to the queue so forbidden lanes are never claimed.
        if self.config.allowed_lanes is not None:
            self.queue.allowed_lanes = frozenset(self.config.allowed_lanes)

    def begin_drain(self) -> None:
        """Stop new claims; in-flight work may finish. Lease reclaim still works."""

        self._draining = True

    def clear_drain(self) -> None:
        self._draining = False

    @property
    def is_draining(self) -> bool:
        return bool(self._draining)

    def health(self) -> dict:
        """Liveness / readiness style worker health (no provider HTTP calls)."""

        from config.runtime_health import STATUS_HEALTHY, STATUS_NOT_READY

        persistence_ok = True
        try:
            store = getattr(self.queue, "store", None)
            if store is not None and hasattr(store, "list_all"):
                store.list_all()
            elif store is not None and hasattr(store, "get"):
                store.get("__health_probe_missing__")
        except Exception:
            persistence_ok = False

        saturated = False
        try:
            from workflow.admission import count_queue_capacity

            counts = count_queue_capacity(self.queue)
            lim = getattr(self.queue, "admission_limits", None)
            max_run = getattr(lim, "max_running_global", None) if lim else None
            if max_run is not None and counts.get("running_global", 0) >= int(max_run):
                saturated = True
        except Exception:
            saturated = False

        ready = STATUS_HEALTHY
        if self._draining or not persistence_ok:
            ready = STATUS_NOT_READY
        return {
            "liveness": STATUS_HEALTHY,
            "readiness": ready,
            "draining": self._draining,
            "saturation": saturated,
            "persistence_ok": persistence_ok,
            "worker_id": self.worker_id,
            "pool_name": self.config.pool_name,
            "allowed_lanes": sorted(self.config.allowed_lanes or ()),
            "max_concurrency": self.config.max_concurrency,
        }

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
        from hitl.errors import (
            ExecutionPermitConsumedError,
            ExecutionPermitExpiredError,
            ExecutionPermitMismatchError,
            ExecutionPermitRevokedError,
        )
        from hitl.permit import PermitService

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
        if self._draining:
            return None
        task = self.queue.dequeue(worker_id=self.worker_id)
        if task is None:
            return None
        return await self.execute(task)

    async def execute(self, task: QueueTask) -> QueueTask:
        lease_id = task.lease_id
        if lease_id is None:
            raise RuntimeError("dequeued task missing lease_id")
        wid = self.worker_id
        if self.engine is not None:
            gate = self.engine.queue_execution_gate(task.workflow_id)
            if gate == "waiting_approval":
                return self.queue.defer_waiting_approval(
                    task.queue_task_id, lease_id, worker_id=wid
                )
            if gate == "cancelled":
                return self.queue.cancel(task.queue_task_id)
            if gate == "completed":
                return self.queue.skip_complete(
                    task.queue_task_id,
                    lease_id,
                    reason="workflow_completed",
                    worker_id=wid,
                )
            if gate == "failed":
                return self.queue.cancel(task.queue_task_id)
        current = self.queue.get(task.queue_task_id)
        if current.metadata.get("cancellation_requested"):
            return self.queue.cancel(task.queue_task_id)
        running = self.queue.start(task.queue_task_id, lease_id, worker_id=wid)
        try:
            self.queue.heartbeat(running.queue_task_id, wid, lease_id)
        except QueueLeaseError:
            raise
        if running.metadata.get("cancellation_requested"):
            return self.queue.abort_running(
                task.queue_task_id, lease_id, worker_id=wid
            )
        handler = self.registry.get(running.execution_key) or self.handler
        ctx = ExecutionContext(
            queue_task_id=running.queue_task_id,
            workflow_id=running.workflow_id,
            task_id=running.task_id,
            execution_key=running.execution_key,
            attempt=running.attempt,
            lease_id=lease_id,
            tenant_id=str(
                running.tenant_id
                or (running.metadata or {}).get("tenant_id")
                or ""
            ),
            user_id=str(
                running.user_id
                or (running.metadata or {}).get("user_id")
                or ""
            ),
            actor_ref=str(
                running.actor_ref
                or (running.metadata or {}).get("actor_ref")
                or ""
            ),
            worker_id=wid,
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
                worker_id=wid,
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
                worker_id=wid,
            )
        return self.queue.ack(running.queue_task_id, lease_id, worker_id=wid)

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
        from workflow.models import STATUS_FAILED

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
