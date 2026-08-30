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
from security.tenant import (
    normalize_tenant_id,
    require_tenant_id,
    scope_execution_key,
    workflow_tenant_id,
)
from workflow.definition import ScheduleSpec
from workflow.admission import (
    AdmissionController,
    AdmissionRejectedError,
    AdmissionLimits,
)
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
from workflow.runtime_role import (
    ROLE_API,
    resolve_runtime_role,
    role_runs_worker_loops,
    workflow_worker_enabled,
)
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
    last_startup_recovery_error: str | None = field(default=None, repr=False)
    last_startup_recovery_result: dict | None = field(default=None, repr=False)
    last_schedule_tick_error: str | None = field(default=None, repr=False)
    runtime_role: str = field(default="combined")
    admission: AdmissionController | None = field(default=None, repr=False)
    _claims_stopped: bool = field(default=False, repr=False)

    def record_startup_recovery_failure(self, exc: BaseException) -> None:
        """Surface startup recovery failure without silent swallow.

        Degraded startup is allowed; the failure must remain diagnosable.
        """
        from security.redaction import redact

        message = redact(str(exc) or type(exc).__name__)
        self.last_startup_recovery_error = message
        obs = self.platform.observability
        if obs is not None:
            ctx = obs.create_context(workflow_id="", task_id="startup")
            obs.emit(
                "workflow.startup_recovery",
                context=ctx,
                component="workflow_service",
                status="failed",
                error_code=type(exc).__name__,
                metadata={"error": message},
            )

    def record_schedule_tick_failure(
        self, exc: BaseException, *, schedule_id: str | None = None
    ) -> None:
        """Surface schedule persist/restore failures (not silent)."""
        from security.redaction import redact

        message = redact(str(exc) or type(exc).__name__)
        self.last_schedule_tick_error = message
        obs = self.platform.observability
        if obs is not None:
            ctx = obs.create_context(workflow_id="", task_id="schedule")
            meta = {"error": message}
            if schedule_id:
                meta["schedule_id"] = schedule_id
            obs.emit(
                "workflow.schedule_tick",
                context=ctx,
                component="workflow_service",
                status="failed",
                error_code=type(exc).__name__,
                metadata=meta,
            )

    async def start_background(self, *, poll_interval: float = 0.25) -> None:
        if self._worker_task is not None:
            return
        if not role_runs_worker_loops(self.runtime_role):
            return
        self._claims_stopped = False
        # Startup: recover + re-enqueue before draining the queue (worker/combined only).
        try:
            self.recover_and_reenqueue_persisted()
        except Exception as exc:
            self.record_startup_recovery_failure(exc)
        self._stop = asyncio.Event()

        async def _loop():
            while self._stop is not None and not self._stop.is_set():
                try:
                    if not self._claims_stopped:
                        await self.tick_schedules()
                        self.reenqueue_due_retries()
                        self.queue.recover_stuck_running(force=False)
                        await self.worker.run_once()
                except Exception:
                    pass
                await asyncio.sleep(poll_interval)

        self._worker_task = asyncio.create_task(_loop())

    async def stop_background(self) -> None:
        self.stop_new_claims()
        if self._stop is not None:
            self._stop.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    def stop_new_claims(self) -> None:
        """Graceful worker shutdown: stop scheduler/queue claims; leases remain."""

        self._claims_stopped = True

    async def tick_schedules(self) -> list[str]:
        from datetime import timedelta

        from side_effects.errors import SideEffectPersistenceUnavailableError

        if self._claims_stopped:
            return []
        launched = []
        store = self.scheduler.store
        claim_fn = getattr(store, "claim_due_window", None)
        complete_fn = getattr(store, "complete_claimed_window", None)
        stale_fn = getattr(store, "list_stale_claims", None)

        async def _fire(due, *, claim=None, window_at=None, key: str):
            payload = dict(due.payload or {})
            tenant_id = payload.get("tenant_id")
            from security.config import DEFAULT_LEGACY_TENANT

            resolved_tenant = require_tenant_id(
                tenant_id if tenant_id not in (None, "") else DEFAULT_LEGACY_TENANT
            )
            result = await self.create_and_enqueue(
                due.workflow_type,
                due.version,
                task_id=f"sched-{due.schedule_id}-{due.run_count}",
                execution_key=key,
                metadata={
                    "schedule_id": due.schedule_id,
                    "trigger": "scheduled",
                    "execution_lane": "scheduled",
                    **payload,
                },
                tenant_id=resolved_tenant,
                priority=PRIORITY_NORMAL,
                execution_lane="scheduled",
            )
            if claim is not None and complete_fn is not None and window_at is not None:
                stamp = utc_now()
                next_run = window_at
                enabled = due.enabled
                if due.interval_seconds:
                    next_run = stamp + timedelta(seconds=float(due.interval_seconds))
                else:
                    enabled = False
                complete_fn(
                    due.schedule_id,
                    claim_token=claim.claim_token,
                    claimed_window_at=window_at,
                    execution_key=key,
                    next_run_at=next_run,
                    enabled=enabled,
                    now=stamp,
                )
            else:
                self.scheduler.mark_enqueued(due.schedule_id, execution_key=key)
            launched.append(result["workflow_id"])
            obs = self.platform.observability
            if obs is not None:
                from observability.helpers import safe_emit

                safe_emit(
                    obs,
                    "workflow.schedule_claimed",
                    context=obs.create_context(workflow_id=result["workflow_id"]),
                    component="scheduler",
                    status="enqueued",
                    metadata={
                        "schedule_id": due.schedule_id,
                        "execution_key": key,
                        "tenant_id": resolved_tenant,
                    },
                )

        # Recover claim→enqueue crash window (expired claims).
        if callable(stale_fn):
            try:
                for stale in stale_fn(utc_now()):
                    due = stale.state
                    window = stale.claimed_window_at
                    payload = dict(due.payload or {})
                    if due.workflow_type == "commerce.reconcile":
                        tenant = str(payload.get("tenant_id") or "legacy-default")
                        key = f"commerce-reconcile:{tenant}:{int(window.timestamp())}"
                    elif due.workflow_type == "payments.reconcile":
                        tenant = str(payload.get("tenant_id") or "legacy-default")
                        key = f"payments-reconcile:{tenant}:{int(window.timestamp())}"
                    else:
                        from dataclasses import replace

                        key = self.scheduler.execution_key_for(
                            replace(due, next_run_at=window)
                        )
                    try:
                        await _fire(
                            due, claim=stale, window_at=window, key=key
                        )
                    except Exception:
                        continue
            except SideEffectPersistenceUnavailableError as exc:
                self.record_schedule_tick_failure(exc)

        try:
            due_list = self.scheduler.due()
        except SideEffectPersistenceUnavailableError as exc:
            self.record_schedule_tick_failure(exc)
            return launched

        for due in due_list:
            payload = dict(due.payload or {})
            window = due.next_run_at
            if due.workflow_type == "commerce.reconcile":
                tenant = str(payload.get("tenant_id") or "legacy-default")
                key = f"commerce-reconcile:{tenant}:{int(window.timestamp())}"
            elif due.workflow_type == "payments.reconcile":
                tenant = str(payload.get("tenant_id") or "legacy-default")
                key = f"payments-reconcile:{tenant}:{int(window.timestamp())}"
            else:
                key = self.scheduler.execution_key_for(due)
            claim = None
            if callable(claim_fn):
                claim = claim_fn(
                    due.schedule_id,
                    expected_next_run_at=window,
                    now=utc_now(),
                )
                if claim is None:
                    continue
            try:
                await _fire(
                    due,
                    claim=claim,
                    window_at=window if claim is not None else None,
                    key=key,
                )
            except SideEffectPersistenceUnavailableError as exc:
                self.record_schedule_tick_failure(exc, schedule_id=due.schedule_id)
                continue
            except AdmissionRejectedError:
                continue
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
        request_id: str | None = None,
        user_id: str | None = None,
        actor_ref: str | None = None,
    ) -> dict:
        definition = self.definitions.get(workflow_type, version)
        task_id = task_id or str(uuid.uuid4())
        state = self.platform.create_instance(
            definition,
            task_id=task_id,
            execution_key=execution_key,
            metadata=metadata,
            tenant_id=tenant_id,
            request_id=request_id,
            user_id=user_id,
            actor_ref=actor_ref,
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
        request_id: str | None = None,
        user_id: str | None = None,
        actor_ref: str | None = None,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        created = self.create_workflow(
            workflow_type,
            version,
            task_id=task_id,
            metadata=metadata,
            sync=True,
            tenant_id=tenant,
            request_id=request_id,
            user_id=user_id,
            actor_ref=actor_ref,
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
        request_id: str | None = None,
        user_id: str | None = None,
        actor_ref: str | None = None,
        execution_lane: str | None = None,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        meta_in = dict(metadata or {})
        if execution_lane:
            meta_in.setdefault("execution_lane", execution_lane)
        admission = self.admission or AdmissionController(
            observability=self.platform.observability
        )
        admission.require_enqueue(
            self.queue,
            tenant_id=tenant,
            priority=priority,
            execution_lane=execution_lane,
            metadata=meta_in,
        )
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
                    execution_lane=execution_lane or meta_in.get("execution_lane"),
                )

        created = self.create_workflow(
            workflow_type,
            version,
            task_id=task_id,
            execution_key=execution_key,
            metadata=meta_in,
            sync=False,
            tenant_id=tenant,
            request_id=request_id,
            user_id=user_id,
            actor_ref=actor_ref,
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
                    execution_lane=execution_lane or meta_in.get("execution_lane"),
                )
        return self.enqueue_existing(
            created["workflow_id"],
            priority=priority,
            timeout_seconds=timeout_seconds,
            metadata={
                "workflow_type": workflow_type,
                "version": version,
                **({"execution_lane": execution_lane} if execution_lane else {}),
            },
            execution_lane=execution_lane,
        )

    def _return_existing_for_enqueue(
        self,
        workflow_id: str,
        *,
        priority: str,
        timeout_seconds: float | None,
        workflow_type: str,
        version: str,
        execution_lane: str | None = None,
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
            metadata={
                "workflow_type": workflow_type,
                "version": version,
                **({"execution_lane": execution_lane} if execution_lane else {}),
            },
            idempotent=True,
            execution_lane=execution_lane,
        )

    def enqueue_existing(
        self,
        workflow_id: str,
        *,
        priority: str = PRIORITY_NORMAL,
        timeout_seconds: float | None = None,
        metadata=None,
        idempotent: bool = False,
        execution_lane: str | None = None,
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
        user_id = str(getattr(state, "user_id", None) or (state.metadata or {}).get("user_id") or "")
        actor_ref = str(
            getattr(state, "actor_ref", None) or (state.metadata or {}).get("actor_ref") or ""
        )
        meta = metadata or {
            "workflow_type": state.workflow_type or "",
            "version": state.definition_version or "",
            "tenant_id": tenant,
        }
        meta = dict(meta)
        meta.setdefault("tenant_id", tenant)
        if user_id:
            meta.setdefault("user_id", user_id)
        if actor_ref:
            meta.setdefault("actor_ref", actor_ref)
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
                tenant_id=tenant,
                user_id=user_id,
                actor_ref=actor_ref,
                execution_lane=execution_lane,
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
                tenant_id=tenant,
                user_id=user_id,
                actor_ref=actor_ref,
                execution_lane=execution_lane,
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
          reclaim stuck queue RUNNING → load persisted → recover interrupted
          → skip analyze orphans (no workflow_type) → re-enqueue durable runnable
        """

        self.last_startup_recovery_error = None
        recovered: list[str] = []
        reenqueued: list[str] = []
        skipped: list[dict] = []
        queue_reclaimed: list[str] = []

        try:
            queue_reclaimed = list(self.queue.recover_stuck_running(force=False))
        except Exception as exc:
            self.record_startup_recovery_failure(exc)
            raise

        try:
            workflows = list(self.state_manager._store.list_all())
        except Exception as exc:
            self.record_startup_recovery_failure(exc)
            raise

        # Deterministic order
        workflows.sort(key=lambda w: (w.created_at, w.workflow_id))

        for wf in workflows:
            wid = wf.workflow_id
            try:
                state = self.state_manager.get(wid)
            except Exception:
                skipped.append({"workflow_id": wid, "reason": "load_failed"})
                continue

            # Path A analyze orphans: durable state without DAG definition.
            # Demote interrupted steps for consistency; never Path-B re-enqueue.
            if not state.workflow_type:
                if state.status == STATUS_RUNNING:
                    try:
                        self.platform.recover_after_restart(wid)
                        recovered.append(wid)
                    except Exception:
                        skipped.append(
                            {"workflow_id": wid, "reason": "analyze_recover_failed"}
                        )
                        continue
                skipped.append(
                    {"workflow_id": wid, "reason": "not_durable_definition"}
                )
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
        result = {
            "recovered": recovered,
            "reenqueued": reenqueued,
            "skipped": skipped,
            "queue_reclaimed": queue_reclaimed,
        }
        self.last_startup_recovery_result = result
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
                    "queue_reclaimed_count": len(queue_reclaimed),
                },
            )
        return result

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
        user_id = str(
            getattr(state, "user_id", None) or (state.metadata or {}).get("user_id") or ""
        )
        actor_ref = str(
            getattr(state, "actor_ref", None)
            or (state.metadata or {}).get("actor_ref")
            or ""
        )
        self.registry.register(scoped_key, self._make_handler(workflow_id))
        task = self.queue.enqueue(
            workflow_id=workflow_id,
            task_id=state.task_id,
            execution_key=scoped_key,
            metadata={
                "resume": True,
                "tenant_id": tenant,
                **({"user_id": user_id} if user_id else {}),
                **({"actor_ref": actor_ref} if actor_ref else {}),
            },
            tenant_id=tenant,
            user_id=user_id,
            actor_ref=actor_ref,
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
    schedule_store=None,
    task_queue_store=None,
    runtime_role: str | None = None,
    env: dict | None = None,
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
    role = runtime_role or resolve_runtime_role(env)
    limits = AdmissionLimits.from_env(env)
    admission = AdmissionController(
        limits,
        observability=observability or platform.observability,
    )
    from task_queue.lanes import LaneCapacityConfig, parse_worker_lanes

    lane_cfg = LaneCapacityConfig.from_env(env)
    source = env if env is not None else {}
    import os as _os

    worker_lanes = parse_worker_lanes(
        source.get("WORKER_LANES")
        if hasattr(source, "get")
        else _os.environ.get("WORKER_LANES")
    )
    if queue is not None:
        tq = queue
    elif task_queue_store is not None:
        tq = TaskQueue(
            store=task_queue_store,
            admission_limits=limits,
            lane_config=lane_cfg,
            allowed_lanes=worker_lanes,
        )
    else:
        tq = TaskQueue(
            admission_limits=limits,
            lane_config=lane_cfg,
            allowed_lanes=worker_lanes,
        )
    if observability is not None:
        tq.observability = observability
    if getattr(tq, "admission_limits", None) is None:
        tq.admission_limits = limits
    if getattr(tq, "lane_config", None) is None:
        tq.lane_config = lane_cfg
    if getattr(tq, "allowed_lanes", None) is None:
        tq.allowed_lanes = worker_lanes
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
        scheduler=WorkflowScheduler(store=schedule_store),
        definitions=defs,
        state_manager=sm,
        runtime_role=role,
        admission=admission,
    )


# Re-export for callers still importing from workflow.service
# workflow_worker_enabled is imported from workflow.runtime_role above.
