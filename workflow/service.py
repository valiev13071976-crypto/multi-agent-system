"""Production workflow service: create / enqueue / status / cancel / schedule."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field

from task_queue.models import PRIORITY_NORMAL
from task_queue.queue import TaskQueue
from task_queue.worker import ExecutionContextRegistry, TaskWorker, WorkerConfig
from workflow.definition import ScheduleSpec, WorkflowDefinition
from workflow.models import STATUS_QUEUED, STATUS_RUNNING, utc_now
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

    async def start_background(self, *, poll_interval: float = 0.25) -> None:
        if self._worker_task is not None:
            return
        self._stop = asyncio.Event()

        async def _loop():
            while self._stop is not None and not self._stop.is_set():
                try:
                    await self.tick_schedules()
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
            key = self.scheduler.execution_key_for(due)
            try:
                result = await self.create_and_enqueue(
                    due.workflow_type,
                    due.version,
                    task_id=f"sched-{due.schedule_id}-{due.run_count}",
                    execution_key=key,
                    metadata={"schedule_id": due.schedule_id, **dict(due.payload)},
                )
                self.scheduler.mark_enqueued(due.schedule_id, execution_key=key)
                launched.append(result["workflow_id"])
            except Exception:
                # duplicate execution_key or definition missing — skip silently
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
    ) -> dict:
        definition = self.definitions.get(workflow_type, version)
        task_id = task_id or str(uuid.uuid4())
        state = self.platform.create_instance(
            definition,
            task_id=task_id,
            execution_key=execution_key,
            metadata=metadata,
        )
        return {
            "workflow_id": state.workflow_id,
            "task_id": state.task_id,
            "status": state.status,
            "execution_key": state.execution_key,
            "sync": sync,
        }

    async def create_and_run_sync(
        self,
        workflow_type: str,
        version: str,
        *,
        task_id: str | None = None,
        metadata=None,
    ) -> dict:
        created = self.create_workflow(
            workflow_type, version, task_id=task_id, metadata=metadata, sync=True
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
    ) -> dict:
        created = self.create_workflow(
            workflow_type,
            version,
            task_id=task_id,
            execution_key=execution_key,
            metadata=metadata,
            sync=False,
        )
        workflow_id = created["workflow_id"]
        self.state_manager.queue(workflow_id)
        self._obs_queue(workflow_id, "workflow.queued")
        exec_key = created["execution_key"]
        self.registry.register(exec_key, self._make_handler(workflow_id))
        task = self.queue.enqueue(
            workflow_id=workflow_id,
            task_id=created["task_id"],
            execution_key=exec_key,
            priority=priority,
            timeout_seconds=timeout_seconds,
            metadata={"workflow_type": workflow_type, "version": version},
        )
        return {
            **created,
            "status": STATUS_QUEUED,
            "queue_task_id": task.queue_task_id,
        }

    def _make_handler(self, workflow_id: str):
        platform = self.platform

        async def _handler(ctx):
            # Recover interrupted running steps if process restarted mid-flight
            platform.recover_after_restart(workflow_id)
            state = platform.state_manager.get(workflow_id)
            if state.status == STATUS_QUEUED:
                platform.state_manager.start(workflow_id)
            return await platform.advance(workflow_id)

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
        # best-effort cancel queue tasks for this workflow
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
        exec_key = f"{state.execution_key}:resume:{int(utc_now().timestamp())}"
        self.registry.register(exec_key, self._make_handler(workflow_id))
        task = self.queue.enqueue(
            workflow_id=workflow_id,
            task_id=state.task_id,
            execution_key=exec_key,
            metadata={"resume": True},
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
