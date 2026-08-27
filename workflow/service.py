"""Production workflow service: create / enqueue / status / cancel / schedule."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field

from task_queue.errors import QueueDuplicateExecutionError
from task_queue.models import PRIORITY_NORMAL
from task_queue.queue import TaskQueue
from task_queue.worker import ExecutionContextRegistry, TaskWorker, WorkerConfig
from security.tenant import normalize_tenant_id, scope_execution_key, workflow_tenant_id
from workflow.definition import ScheduleSpec
from workflow.models import (
    STATUS_PLANNED,
    STATUS_QUEUED,
    STATUS_RETRY_WAIT,
    STATUS_RUNNING,
    STATUS_VALIDATING,
    STATUS_WAITING_APPROVAL,
    TERMINAL_STATUSES,
    utc_now,
)
from workflow.platform import WorkflowPlatform
from workflow.registry import DefinitionRegistry
from workflow.schedule import WorkflowScheduler
from workflow.state_manager import StateManager


@dataclass
class WorkflowRuntimeBundle:
    platform: WorkflowPlatform
    queue: TaskQueue
    worker: TaskWorker
    registry: ExecutionContextRegistry
    scheduler: WorkflowScheduler
    definitions: DefinitionRegistry
    state_manager: StateManager
    _worker_task: asyncio.Task | None = field(default=None, repr=False)
    _scheduler_task: asyncio.Task | None = field(default=None, repr=False)
    _stop: asyncio.Event | None = field(default=None, repr=False)
    _startup_recovery_ran: bool = field(default=False, repr=False)

    async def start_background(self, *, poll_interval: float = 0.25) -> None:
        if self._worker_task is not None:
            return
        # Startup: recover + re-enqueue before draining the queue.
        self.recover_and_reenqueue_persisted()
        self._stop = asyncio.Event()

        async def _loop():
            while self._stop is not None and not self._stop.is_set():
                try:
                    await self.tick_schedules()
                    # Promote due retry_wait workflows into the queue.
                    self.reenqueue_due_retries()
                    await self.worker.run_once()
                except Exception:
                    pass
                await asyncio.sleep(poll_interval)

        self._worker_task = asyncio.create_task(_loop())

    async def stop_background(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def tick_schedules(self) -> list[str]:
        launched = []
        for due in self.scheduler.due():
            payload = dict(due.payload or {})
            # Prefer commerce-specific idempotency key when present
            if due.workflow_type == "commerce.reconcile":
                tenant = str(payload.get("tenant_id") or "legacy-default")
                window = int(due.next_run_at.timestamp())
                key = f"commerce-reconcile:{tenant}:{window}"
            elif due.workflow_type == "payments.reconcile":
                tenant = str(payload.get("tenant_id") or "legacy-default")
                window = int(due.next_run_at.timestamp())
                key = f"payments-reconcile:{tenant}:{window}"
            else:
                key = self.scheduler.execution_key_for(due)
            tenant_id = payload.get("tenant_id")
            try:
                result = await self.create_and_enqueue(
                    due.workflow_type,
                    due.version,
                    task_id=f"sched-{due.schedule_id}-{due.run_count}",
                    execution_key=key,
                    metadata={"schedule_id": due.schedule_id, **payload},
                    tenant_id=str(tenant_id) if tenant_id else None,
                )
                self.scheduler.mark_enqueued(due.schedule_id, execution_key=key)
                launched.append(result["workflow_id"])
            except Exception:
                continue
        return launched

    def create_workflow(
        self,
        workflow_type: str,
        version: str,
        *,
        task_id: str | None = None,
        execution_key: str | None = None,
        metadata=None,
        sync: bool = False,
        tenant_id: str | None = None,
    ) -> dict:
        definition = self.definitions.get(workflow_type, version)
        task_id = task_id or str(uuid.uuid4())
        state = self.platform.create_instance(
            definition,
            task_id=task_id,
            execution_key=execution_key,
            metadata=metadata,
            tenant_id=tenant_id,
        )
        return {
            "workflow_id": state.workflow_id,
            "task_id": state.task_id,
            "status": state.status,
            "execution_key": state.execution_key,
            "tenant_id": workflow_tenant_id(state),
            "sync": sync,
        }

    async def create_and_run_sync(
        self,
        workflow_type: str,
        version: str,
        *,
        task_id: str | None = None,
        metadata=None,
        tenant_id: str | None = None,
    ) -> dict:
        created = self.create_workflow(
            workflow_type,
            version,
            task_id=task_id,
            metadata=metadata,
            sync=True,
            tenant_id=tenant_id,
        )
        self.state_manager.start(created["workflow_id"])
        result = await self.platform.advance(created["workflow_id"])
        return {**created, **result, **self.platform.status_payload(created["workflow_id"])}

    async def create_and_enqueue(
        self,
        workflow_type: str,
        version: str,
        *,
        task_id: str | None = None,
        execution_key: str | None = None,
        metadata=None,
        priority: str = PRIORITY_NORMAL,
        timeout_seconds: float | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        tenant = normalize_tenant_id(tenant_id)
        # Idempotency BEFORE creating a WorkflowInstance (tenant-scoped).
        if execution_key:
            existing = self.state_manager.find_by_execution_key(
                execution_key, tenant_id=tenant
            )
            if existing is not None:
                return self._return_existing_for_enqueue(
                    existing.workflow_id,
                    priority=priority,
                    timeout_seconds=timeout_seconds,
                    workflow_type=workflow_type,
                    version=version,
                )

        created = self.create_workflow(
            workflow_type,
            version,
            task_id=task_id,
            execution_key=execution_key,
            metadata=metadata,
            sync=False,
            tenant_id=tenant,
        )
        if execution_key:
            winner = self.state_manager.find_by_execution_key(
                execution_key, tenant_id=tenant
            )
            if winner is not None and winner.workflow_id != created["workflow_id"]:
                try:
                    if self.state_manager.get(created["workflow_id"]).status not in TERMINAL_STATUSES:
                        self.state_manager.cancel(created["workflow_id"])
                except Exception:
                    pass
                return self._return_existing_for_enqueue(
                    winner.workflow_id,
                    priority=priority,
                    timeout_seconds=timeout_seconds,
                    workflow_type=workflow_type,
                    version=version,
                )
        return self.enqueue_existing(
            created["workflow_id"],
            priority=priority,
            timeout_seconds=timeout_seconds,
            metadata={"workflow_type": workflow_type, "version": version},
        )

    def _return_existing_for_enqueue(
        self,
        workflow_id: str,
        *,
        priority: str,
        timeout_seconds: float | None,
        workflow_type: str,
        version: str,
    ) -> dict:
        state = self.state_manager.get(workflow_id)
        if state.status in TERMINAL_STATUSES:
            return {
                "workflow_id": state.workflow_id,
                "task_id": state.task_id,
                "status": state.status,
                "execution_key": state.execution_key,
                "queue_task_id": None,
                "idempotent": True,
            }
        if state.status == STATUS_WAITING_APPROVAL:
            return {
                "workflow_id": state.workflow_id,
                "task_id": state.task_id,
                "status": state.status,
                "execution_key": state.execution_key,
                "queue_task_id": None,
                "idempotent": True,
            }
        return self.enqueue_existing(
            workflow_id,
            priority=priority,
            timeout_seconds=timeout_seconds,
            metadata={"workflow_type": workflow_type, "version": version},
            idempotent=True,
        )

    def enqueue_existing(
        self,
        workflow_id: str,
        *,
        priority: str = PRIORITY_NORMAL,
        timeout_seconds: float | None = None,
        metadata=None,
        idempotent: bool = False,
    ) -> dict:
        """Enqueue an existing workflow without creating a new WorkflowInstance."""

        state = self.state_manager.get(workflow_id)
        if state.status in TERMINAL_STATUSES:
            return {
                "workflow_id": state.workflow_id,
                "task_id": state.task_id,
                "status": state.status,
                "execution_key": state.execution_key,
                "queue_task_id": None,
                "idempotent": True,
            }
        if state.status == STATUS_WAITING_APPROVAL:
            return {
                "workflow_id": state.workflow_id,
                "task_id": state.task_id,
                "status": state.status,
                "execution_key": state.execution_key,
                "queue_task_id": None,
                "idempotent": True,
            }
        if state.status == STATUS_RETRY_WAIT:
            if state.next_retry_at is not None and state.next_retry_at > utc_now():
                return {
                    "workflow_id": state.workflow_id,
                    "task_id": state.task_id,
                    "status": state.status,
                    "execution_key": state.execution_key,
                    "queue_task_id": None,
                    "idempotent": True,
                    "reason": "retry_not_due",
                }
            self.state_manager.clear_retry_wait(workflow_id)
            if self.state_manager.get(workflow_id).status == STATUS_RETRY_WAIT:
                self.state_manager.queue(workflow_id)
        elif state.status in {STATUS_PLANNED, STATUS_VALIDATING}:
            self.state_manager.queue(workflow_id)
        # STATUS_QUEUED / STATUS_RUNNING: leave status; still enqueue worker work.

        state = self.state_manager.get(workflow_id)
        exec_key = state.execution_key
        tenant = workflow_tenant_id(state)
        scoped_key = scope_execution_key(tenant, exec_key)
        meta = metadata or {
            "workflow_type": state.workflow_type or "",
            "version": state.definition_version or "",
            "tenant_id": tenant,
        }
        handler = self._make_handler(workflow_id)
        self.registry.register(scoped_key, handler)
        self._obs_queue(workflow_id, "workflow.queued")
        try:
            task = self.queue.enqueue(
                workflow_id=workflow_id,
                task_id=state.task_id,
                execution_key=scoped_key,
                priority=priority,
                timeout_seconds=timeout_seconds,
                metadata=meta,
            )
        except QueueDuplicateExecutionError:
            # Prior queue task is terminal, but workflow still needs work
            # (retry_wait wake / continue after ack). Mint a unique wake key.
            wake_key = f"{scoped_key}#wake-{uuid.uuid4().hex}"
            self.registry.register(wake_key, handler)
            task = self.queue.enqueue(
                workflow_id=workflow_id,
                task_id=state.task_id,
                execution_key=wake_key,
                priority=priority,
                timeout_seconds=timeout_seconds,
                metadata={**dict(meta), "wake_of": scoped_key},
            )
            return {
                "workflow_id": state.workflow_id,
                "task_id": state.task_id,
                "status": self.state_manager.get(workflow_id).status,
                "execution_key": exec_key,
                "queue_task_id": task.queue_task_id,
                "idempotent": False,
                "wake": True,
            }
        return {
            "workflow_id": state.workflow_id,
            "task_id": state.task_id,
            "status": self.state_manager.get(workflow_id).status,
            "execution_key": exec_key,
            "queue_task_id": task.queue_task_id,
            "idempotent": idempotent,
        }

    def recover_and_reenqueue_persisted(self) -> dict:
        """Production startup recovery for durable workflows + empty TaskQueue.

        Flow:
          load persisted → recover interrupted running → identify runnable/due
          → re-enqueue existing → (caller starts worker)
        """

        recovered: list[str] = []
        reenqueued: list[str] = []
        skipped: list[dict] = []
        try:
            workflows = list(self.state_manager._store.list_all())
        except Exception:
            return {
                "recovered": recovered,
                "reenqueued": reenqueued,
                "skipped": skipped,
            }

        # Deterministic order
        workflows.sort(key=lambda w: (w.created_at, w.workflow_id))

        for wf in workflows:
            wid = wf.workflow_id
            try:
                state = self.state_manager.get(wid)
            except Exception:
                continue

            if state.status == STATUS_RUNNING:
                state = self.platform.recover_after_restart(wid)
                recovered.append(wid)

            state = self.state_manager.get(wid)

            if state.status in TERMINAL_STATUSES:
                skipped.append({"workflow_id": wid, "reason": "terminal"})
                continue
            if state.status == STATUS_WAITING_APPROVAL:
                skipped.append({"workflow_id": wid, "reason": "waiting_approval"})
                continue
            if state.status == STATUS_RETRY_WAIT:
                if state.next_retry_at is not None and state.next_retry_at > utc_now():
                    skipped.append({"workflow_id": wid, "reason": "retry_not_due"})
                    continue

            # runnable: queued / recovered→queued / due retry_wait / planned
            if state.status in {
                STATUS_QUEUED,
                STATUS_PLANNED,
                STATUS_RETRY_WAIT,
                STATUS_RUNNING,
                STATUS_VALIDATING,
            }:
                result = self.enqueue_existing(wid)
                if result.get("queue_task_id"):
                    reenqueued.append(wid)
                else:
                    skipped.append(
                        {
                            "workflow_id": wid,
                            "reason": result.get("reason") or result.get("status"),
                        }
                    )
            else:
                skipped.append({"workflow_id": wid, "reason": state.status})

        self._startup_recovery_ran = True
        obs = self.platform.observability
        if obs is not None:
            ctx = obs.create_context(workflow_id="", task_id="startup")
            obs.emit(
                "workflow.startup_recovery",
                context=ctx,
                component="workflow_service",
                status="recovered",
                metadata={
                    "recovered_count": len(recovered),
                    "reenqueued_count": len(reenqueued),
                    "skipped_count": len(skipped),
                },
            )
        return {
            "recovered": recovered,
            "reenqueued": reenqueued,
            "skipped": skipped,
        }

    def reenqueue_due_retries(self) -> list[str]:
        """Background helper: enqueue retry_wait workflows whose next_retry_at is due."""

        launched = []
        try:
            waiting = self.state_manager._store.list_by_status(STATUS_RETRY_WAIT)
        except Exception:
            return launched
        now = utc_now()
        for wf in waiting:
            if wf.next_retry_at is not None and wf.next_retry_at > now:
                continue
            result = self.enqueue_existing(wf.workflow_id)
            if result.get("queue_task_id"):
                launched.append(wf.workflow_id)
        return launched

    def _make_handler(self, workflow_id: str):
        platform = self.platform

        async def _handler(ctx):
            platform.recover_after_restart(workflow_id)
            state = platform.state_manager.get(workflow_id)
            if state.status == STATUS_QUEUED:
                platform.state_manager.start(workflow_id)
            last = None
            # One DAG/batch step per advance call — each extract slice stays bounded.
            # Drain until terminal/blocked so demos still finish in one worker tick.
            for _ in range(200):
                state = platform.state_manager.get(workflow_id)
                if state.status in TERMINAL_STATUSES:
                    break
                if state.status in {STATUS_WAITING_APPROVAL, STATUS_RETRY_WAIT}:
                    break
                last = await platform.advance(workflow_id, max_steps=1)
                if not list(last.get("executed") or ()):
                    break
            return last or {
                "workflow_id": workflow_id,
                "status": platform.state_manager.get(workflow_id).status,
            }

        return _handler

    def _obs_queue(self, workflow_id: str, event: str) -> None:
        obs = self.platform.observability
        if obs is None:
            return
        ctx = obs.context_for_workflow(workflow_id) or obs.create_context(
            workflow_id=workflow_id
        )
        obs.emit(event, context=ctx, component="workflow_service", status="queued")

    def get_status(self, workflow_id: str) -> dict:
        return self.platform.status_payload(workflow_id)

    def cancel(self, workflow_id: str) -> dict:
        result = self.platform.cancel(workflow_id)
        try:
            for task in self.queue.store.list_all():  # type: ignore[attr-defined]
                if task.workflow_id == workflow_id and task.status not in {
                    "completed",
                    "cancelled",
                    "dead_lettered",
                }:
                    try:
                        self.queue.cancel(task.queue_task_id)
                    except Exception:
                        pass
        except Exception:
            pass
        return result

    def register_schedule(self, spec: ScheduleSpec):
        return self.scheduler.register(spec)

    async def resume_after_approval(self, workflow_id: str) -> dict:
        """After HITL approve → re-queue for worker continuation."""

        state = self.state_manager.get(workflow_id)
        if state.status == STATUS_RUNNING:
            self.state_manager.queue(workflow_id)
        # Resume uses a distinct queue key so terminal history of original key is OK.
        exec_key = f"{state.execution_key}:resume:{int(utc_now().timestamp())}"
        tenant = workflow_tenant_id(state)
        scoped_key = scope_execution_key(tenant, exec_key)
        self.registry.register(scoped_key, self._make_handler(workflow_id))
        task = self.queue.enqueue(
            workflow_id=workflow_id,
            task_id=state.task_id,
            execution_key=scoped_key,
            metadata={"resume": True, "tenant_id": tenant},
        )
        self._obs_queue(workflow_id, "workflow.resumed_queued")
        return {
            "workflow_id": workflow_id,
            "queue_task_id": task.queue_task_id,
            "status": self.state_manager.get(workflow_id).status,
        }


def build_workflow_runtime(
    *,
    state_manager: StateManager | None = None,
    workflow_engine=None,
    observability=None,
    autonomy_gate=None,
    hitl_service=None,
    queue: TaskQueue | None = None,
    definitions: DefinitionRegistry | None = None,
) -> WorkflowRuntimeBundle:
    sm = state_manager or (
        workflow_engine.state_manager if workflow_engine is not None else StateManager()
    )
    defs = definitions or DefinitionRegistry()
    platform = WorkflowPlatform(
        sm,
        defs,
        observability=observability
        or (getattr(workflow_engine, "observability", None) if workflow_engine else None),
        autonomy_gate=autonomy_gate
        or (getattr(workflow_engine, "autonomy_gate", None) if workflow_engine else None),
        hitl_service=hitl_service
        or (getattr(workflow_engine, "hitl_service", None) if workflow_engine else None),
        side_effect_executor=getattr(workflow_engine, "side_effect_executor", None)
        if workflow_engine
        else None,
        workflow_engine=workflow_engine,
    )
    registry = ExecutionContextRegistry()
    tq = queue or TaskQueue()
    if observability is not None:
        tq.observability = observability
    worker = TaskWorker(
        tq,
        engine=workflow_engine,
        registry=registry,
        config=WorkerConfig(max_concurrency=1),
    )
    return WorkflowRuntimeBundle(
        platform=platform,
        queue=tq,
        worker=worker,
        registry=registry,
        scheduler=WorkflowScheduler(),
        definitions=defs,
        state_manager=sm,
    )


def workflow_worker_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("WORKFLOW_WORKER_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}
