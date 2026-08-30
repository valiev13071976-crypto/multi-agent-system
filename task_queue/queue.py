import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Mapping

from security.redaction import redact
from task_queue.errors import (
    QueueDuplicateExecutionError,
    QueueLeaseError,
    QueueTaskNotFoundError,
    QueueTenantOwnershipError,
    QueueTransitionError,
)
from task_queue.models import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    PRIORITY_NORMAL,
    PRIORITY_RANK,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DEAD_LETTERED,
    STATUS_LEASED,
    STATUS_QUEUED,
    STATUS_RETRY_WAIT,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    QueueTask,
    utc_now,
)
from task_queue.retry import RetryPolicy, is_retryable
from task_queue.store import InMemoryTaskQueueStore, TaskQueueStore

DEFERRED_UNTIL = datetime(9999, 12, 31, tzinfo=timezone.utc)

FORBIDDEN_METADATA_KEYS = (
    "prompt",
    "authorization",
    "api_key",
    "cookie",
    "cookies",
    "password",
    "secret",
    "token",
    "encryption_key",
    "panda_encryption_key",
    "raw_body",
    "raw_provider",
    "expert_raw",
    "search_raw",
)


def sanitize_metadata(metadata: Mapping | None) -> dict:
    cleaned = {}
    for key, value in dict(metadata or {}).items():
        lowered = str(key).lower()
        if any(part in lowered for part in FORBIDDEN_METADATA_KEYS):
            continue
        if isinstance(value, str):
            cleaned[str(key)] = redact(value)
        elif isinstance(value, Mapping):
            cleaned[str(key)] = sanitize_metadata(value)
        else:
            cleaned[str(key)] = value
    return cleaned


class TaskQueue:
    def __init__(
        self,
        store: TaskQueueStore | None = None,
        retry_policy: RetryPolicy | None = None,
        *,
        lease_seconds: float = 300.0,
        now_fn=None,
        admission_limits=None,
        lane_config=None,
        allowed_lanes=None,
    ):
        self.store = store or InMemoryTaskQueueStore()
        self.retry_policy = retry_policy or RetryPolicy()
        self.lease_seconds = float(lease_seconds)
        self._now = now_fn or utc_now
        self.observability = None
        self.admission_limits = admission_limits
        self.lane_config = lane_config
        self.allowed_lanes = allowed_lanes
        self._lock = threading.RLock()

    def _obs_emit(self, event_type: str, task=None, **kwargs):
        from observability.helpers import safe_emit

        if self.observability is None:
            return
        workflow_id = getattr(task, "workflow_id", "") if task is not None else ""
        task_id = getattr(task, "task_id", "") if task is not None else ""
        parent = (
            self.observability.context_for_workflow(workflow_id) if workflow_id else None
        )
        span = (
            self.observability.child_span(parent)
            if parent is not None
            else self.observability.create_context(
                workflow_id=workflow_id, task_id=task_id
            )
        )
        meta = dict(kwargs.pop("metadata", None) or {})
        if task is not None:
            meta.setdefault("queue_task_id", getattr(task, "queue_task_id", ""))
            meta.setdefault("workflow_id", workflow_id)
            meta.setdefault("tenant_id", getattr(task, "tenant_id", "") or "")
            meta.setdefault("worker_id", getattr(task, "worker_id", "") or "")
            meta.setdefault("attempt", getattr(task, "attempt", 0))
            if getattr(task, "lease_id", None):
                meta.setdefault("lease_id", task.lease_id)
        safe_emit(
            self.observability,
            event_type,
            context=span,
            component="queue",
            metadata=meta,
            **kwargs,
        )

    def now(self) -> datetime:
        return self._now()

    def get(self, queue_task_id: str) -> QueueTask:
        task = self.store.get(queue_task_id)
        if task is None:
            raise QueueTaskNotFoundError(queue_task_id)
        return task

    def get_for_tenant(self, queue_task_id: str, tenant_id: str) -> QueueTask | None:
        store = self.store
        if hasattr(store, "get_for_tenant"):
            return store.get_for_tenant(queue_task_id, tenant_id)
        tid = str(tenant_id or "").strip()
        if not tid:
            return None
        task = store.get(queue_task_id)
        if task is None:
            return None
        if str(getattr(task, "tenant_id", "") or "").strip() != tid:
            return None
        return task

    def enqueue(
        self,
        *,
        workflow_id: str,
        task_id: str,
        execution_key: str,
        priority: str = PRIORITY_NORMAL,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
        metadata: Mapping | None = None,
        queue_task_id: str | None = None,
        now: datetime | None = None,
        tenant_id: str = "",
        user_id: str = "",
        actor_ref: str = "",
        execution_lane: str | None = None,
    ) -> QueueTask:
        from task_queue.lanes import resolve_execution_lane

        with self._lock:
            existing = self.store.find_by_execution_key(execution_key)
            active = [item for item in existing if item.status in ACTIVE_STATUSES]
            if active:
                return sorted(
                    active, key=lambda item: (item.created_at, item.queue_task_id)
                )[0]
            terminal = [item for item in existing if item.status in TERMINAL_STATUSES]
            if terminal:
                raise QueueDuplicateExecutionError(execution_key)
            stamp = now or self.now()
            meta = dict(sanitize_metadata(metadata) or {})
            resolved_tenant = str(tenant_id or meta.get("tenant_id") or "")
            resolved_user = str(user_id or meta.get("user_id") or "")
            resolved_actor = str(actor_ref or meta.get("actor_ref") or "")
            if resolved_tenant:
                meta.setdefault("tenant_id", resolved_tenant)
            if resolved_user:
                meta.setdefault("user_id", resolved_user)
            if resolved_actor:
                meta.setdefault("actor_ref", resolved_actor)
            lane = resolve_execution_lane(
                execution_lane=execution_lane,
                priority=priority,
                metadata=meta,
            )
            meta.setdefault("execution_lane", lane)
            task = QueueTask(
                queue_task_id=queue_task_id or str(uuid.uuid4()),
                workflow_id=workflow_id,
                task_id=task_id,
                execution_key=execution_key,
                status=STATUS_QUEUED,
                priority=priority,
                attempt=0,
                max_attempts=int(
                    self.retry_policy.max_attempts
                    if max_attempts is None
                    else max_attempts
                ),
                created_at=stamp,
                available_at=stamp,
                updated_at=stamp,
                timeout_seconds=timeout_seconds,
                metadata=meta,
                tenant_id=resolved_tenant,
                user_id=resolved_user,
                actor_ref=resolved_actor,
                execution_lane=lane,
            )
            self.store.enqueue(task)
            self._obs_emit(
                "queue.enqueued",
                task,
                status="enqueued",
                metadata={"execution_lane": lane},
            )
            try:
                from runtime.metrics import RUNTIME_COUNTERS

                RUNTIME_COUNTERS.inc("enqueue", lane=lane)
            except Exception:
                pass
            return task

    def list_ready(self, *, now: datetime | None = None) -> tuple[QueueTask, ...]:
        stamp = now or self.now()
        ready = []
        for item in self.store.list_ready():
            if item.status in {STATUS_QUEUED, STATUS_RETRY_WAIT}:
                if item.available_at <= stamp:
                    ready.append(item)
            elif item.status == STATUS_LEASED:
                if item.lease_expires_at is not None and item.lease_expires_at <= stamp:
                    ready.append(item)
        ready.sort(
            key=lambda item: (
                -PRIORITY_RANK[item.priority],
                item.available_at,
                item.created_at,
                item.queue_task_id,
            )
        )
        return tuple(ready)

    def dequeue(
        self,
        *,
        worker_id: str = "worker",
        now: datetime | None = None,
        lease_seconds: float | None = None,
        max_running_global: int | None = None,
        max_running_per_tenant: int | None = None,
    ) -> QueueTask | None:
        stamp = now or self.now()
        ttl = self.lease_seconds if lease_seconds is None else float(lease_seconds)
        worker = str(worker_id or "").strip() or "worker"

        # Prefer queue-bound limits (runtime env), then process env.
        if max_running_global is None or max_running_per_tenant is None:
            from workflow.admission import AdmissionLimits

            lim = self.admission_limits or AdmissionLimits.from_env()
            if max_running_global is None:
                max_running_global = lim.max_running_global
            if max_running_per_tenant is None:
                max_running_per_tenant = lim.max_running_per_tenant

        claim_next = getattr(self.store, "claim_next", None)
        if callable(claim_next):
            from task_queue.lanes import LaneCapacityConfig

            lane_cfg = self.lane_config or LaneCapacityConfig.from_env()
            # Skip deadline-expired ready tasks by claiming then aborting.
            for _ in range(8):
                leased = claim_next(
                    worker_id=worker,
                    lease_seconds=ttl,
                    now=stamp,
                    max_running_global=max_running_global,
                    max_running_per_tenant=max_running_per_tenant,
                    allowed_lanes=self.allowed_lanes,
                    lane_config=lane_cfg,
                )
                if leased is None:
                    return None
                from workflow.admission import task_past_deadline

                if task_past_deadline(leased, now=stamp):
                    try:
                        self.start(
                            leased.queue_task_id,
                            leased.lease_id,
                            worker_id=worker,
                            now=stamp,
                        )
                        self.fail(
                            leased.queue_task_id,
                            leased.lease_id,
                            error_code="deadline_expired",
                            worker_id=worker,
                            now=stamp,
                        )
                    except Exception:
                        pass
                    continue
                self._obs_emit(
                    "queue.claimed",
                    leased,
                    status="leased",
                    metadata={"execution_lane": leased.execution_lane},
                )
                try:
                    from runtime.metrics import RUNTIME_COUNTERS

                    RUNTIME_COUNTERS.inc(
                        "claim", lane=getattr(leased, "execution_lane", "") or ""
                    )
                except Exception:
                    pass
                return leased
            return None

        with self._lock:
            ready = self.list_ready(now=stamp)
            if not ready:
                return None
            # Capacity peek for in-memory
            from workflow.admission import count_queue_capacity, task_past_deadline

            counts = count_queue_capacity(self, now=stamp)
            if (
                max_running_global is not None
                and counts["running_global"] >= max_running_global
            ):
                return None
            allowed = self.allowed_lanes
            task = None
            for candidate in ready:
                if allowed is not None and candidate.execution_lane not in allowed:
                    continue
                if task_past_deadline(candidate, now=stamp):
                    continue
                if max_running_per_tenant is not None and candidate.tenant_id:
                    tcounts = count_queue_capacity(
                        self, tenant_id=candidate.tenant_id, now=stamp
                    )
                    if tcounts["running_tenant"] >= max_running_per_tenant:
                        continue
                task = candidate
                break
            if task is None:
                return None
            current = self.store.get(task.queue_task_id)
            if current is None:
                return None
            if current.status in {STATUS_QUEUED, STATUS_RETRY_WAIT}:
                if current.available_at > stamp:
                    return None
            elif current.status == STATUS_LEASED:
                if (
                    current.lease_expires_at is None
                    or current.lease_expires_at > stamp
                ):
                    return None
            else:
                return None
            lease_fields = {
                "lease_id": str(uuid.uuid4()),
                "leased_at": stamp,
                "lease_expires_at": stamp + timedelta(seconds=ttl),
                "worker_id": worker,
                "updated_at": stamp,
                "metadata": sanitize_metadata(
                    {**dict(current.metadata), "worker_id": worker}
                ),
            }
            if current.status == STATUS_LEASED:
                leased = replace(current, **lease_fields)
            else:
                leased = self._transition(current, STATUS_LEASED, **lease_fields)
            self.store.save(leased)
            self._obs_emit("queue.claimed", leased, status="leased")
            try:
                from runtime.metrics import RUNTIME_COUNTERS

                RUNTIME_COUNTERS.inc(
                    "claim", lane=getattr(leased, "execution_lane", "") or ""
                )
            except Exception:
                pass
            return leased

    def heartbeat(
        self,
        queue_task_id: str,
        worker_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: float | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        ttl = self.lease_seconds if lease_seconds is None else float(lease_seconds)
        hb = getattr(self.store, "heartbeat", None)
        if callable(hb):
            renewed = hb(
                queue_task_id,
                worker_id=worker_id,
                lease_id=lease_id,
                lease_seconds=ttl,
                now=stamp,
            )
            if renewed is None:
                raise QueueLeaseError("heartbeat_rejected")
            self._obs_emit("queue.heartbeat", renewed, status=renewed.status)
            return renewed

        with self._lock:
            task = self._require_lease(
                queue_task_id, lease_id, stamp, worker_id=worker_id
            )
            if task.status not in {STATUS_LEASED, STATUS_RUNNING}:
                raise QueueLeaseError("heartbeat_invalid_status")
            renewed = replace(
                task,
                lease_expires_at=stamp + timedelta(seconds=ttl),
                updated_at=stamp,
            )
            self.store.save(renewed)
            self._obs_emit("queue.heartbeat", renewed, status=renewed.status)
            return renewed

    def start(
        self,
        queue_task_id: str,
        lease_id: str,
        *,
        worker_id: str | None = None,
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        with self._lock:
            task = self._require_lease(
                queue_task_id, lease_id, stamp, worker_id=worker_id
            )
            started = self._transition(
                task,
                STATUS_RUNNING,
                attempt=task.attempt + 1,
                started_at=stamp,
                updated_at=stamp,
            )
            self.store.save(started)
            self._obs_emit("queue.started", started, status="started")
            return started

    def ack(
        self,
        queue_task_id: str,
        lease_id: str,
        *,
        worker_id: str | None = None,
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        with self._lock:
            task = self._require_lease(
                queue_task_id, lease_id, stamp, worker_id=worker_id
            )
            done = self._transition(
                task,
                STATUS_COMPLETED,
                completed_at=stamp,
                updated_at=stamp,
                lease_id=None,
                leased_at=None,
                lease_expires_at=None,
                worker_id=None,
            )
            self.store.save(done)
            self._obs_emit("queue.completed", done, status="completed")
            try:
                from runtime.metrics import RUNTIME_COUNTERS

                RUNTIME_COUNTERS.inc(
                    "complete", lane=getattr(done, "execution_lane", "") or ""
                )
            except Exception:
                pass
            return done

    def skip_complete(
        self,
        queue_task_id: str,
        lease_id: str,
        *,
        reason: str,
        worker_id: str | None = None,
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        with self._lock:
            task = self._require_lease(
                queue_task_id, lease_id, stamp, worker_id=worker_id
            )
            done = self._transition(
                task,
                STATUS_COMPLETED,
                completed_at=stamp,
                updated_at=stamp,
                lease_id=None,
                leased_at=None,
                lease_expires_at=None,
                worker_id=None,
                metadata=sanitize_metadata(
                    {**dict(task.metadata), "skipped_reason": reason}
                ),
            )
            self.store.save(done)
            return done

    def fail(
        self,
        queue_task_id: str,
        lease_id: str,
        *,
        error_code: str,
        metadata: Mapping | None = None,
        worker_id: str | None = None,
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        with self._lock:
            task = self._require_lease(
                queue_task_id, lease_id, stamp, worker_id=worker_id
            )
            code = str(error_code)
            extra = sanitize_metadata(metadata)
            extra["last_failure_metadata"] = dict(extra)
            extra["error_code"] = redact(code)
            if is_retryable(code) and task.attempt < task.max_attempts:
                delay = self.retry_policy.delay_seconds(task.attempt)
                waiting = self._transition(
                    task,
                    STATUS_RETRY_WAIT,
                    error_code=code,
                    failed_at=stamp,
                    available_at=stamp + timedelta(seconds=delay),
                    updated_at=stamp,
                    lease_id=None,
                    leased_at=None,
                    lease_expires_at=None,
                    worker_id=None,
                    metadata=sanitize_metadata({**dict(task.metadata), **extra}),
                )
                self.store.save(waiting)
                self._obs_emit(
                    "queue.failed",
                    waiting,
                    status="retry_wait",
                    error_code=code,
                )
                try:
                    from runtime.metrics import RUNTIME_COUNTERS

                    RUNTIME_COUNTERS.inc(
                        "retry", lane=getattr(waiting, "execution_lane", "") or ""
                    )
                    RUNTIME_COUNTERS.inc(
                        "fail", lane=getattr(waiting, "execution_lane", "") or ""
                    )
                except Exception:
                    pass
                return waiting
            return self.dead_letter(
                queue_task_id,
                lease_id,
                error_code=code,
                metadata=extra,
                worker_id=worker_id,
                now=stamp,
            )

    def dead_letter(
        self,
        queue_task_id: str,
        lease_id: str | None = None,
        *,
        error_code: str,
        metadata: Mapping | None = None,
        worker_id: str | None = None,
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        with self._lock:
            task = self.get(queue_task_id)
            if lease_id is not None:
                task = self._require_lease(
                    queue_task_id, lease_id, stamp, worker_id=worker_id
                )
            extra = sanitize_metadata(metadata)
            extra["last_failure_metadata"] = sanitize_metadata(
                extra.get("last_failure_metadata") or extra
            )
            lettered = self._transition(
                task,
                STATUS_DEAD_LETTERED,
                error_code=str(error_code),
                failed_at=stamp,
                completed_at=stamp,
                updated_at=stamp,
                lease_id=None,
                leased_at=None,
                lease_expires_at=None,
                worker_id=None,
                metadata=sanitize_metadata({**dict(task.metadata), **extra}),
            )
            self.store.save(lettered)
            self._obs_emit(
                "queue.dead_lettered",
                lettered,
                status="dead_lettered",
                error_code=str(error_code),
            )
            try:
                from runtime.metrics import RUNTIME_COUNTERS

                RUNTIME_COUNTERS.inc(
                    "dlq", lane=getattr(lettered, "execution_lane", "") or ""
                )
                RUNTIME_COUNTERS.inc(
                    "fail", lane=getattr(lettered, "execution_lane", "") or ""
                )
            except Exception:
                pass
            return lettered

    def redrive_dead_letter(
        self,
        queue_task_id: str,
        *,
        actor_ref: str,
        tenant_id: str,
        now: datetime | None = None,
    ) -> QueueTask:
        """Operator redrive: DLQ → queued. Does NOT bypass approval — just requeues.

        Fail-closed on tenant mismatch. Preserves execution_key / idempotency_key
        metadata; clears lease; increments attempt.
        """

        from task_queue.errors import QueueError

        stamp = now or self.now()
        tid = str(tenant_id or "").strip()
        if not tid:
            raise QueueTenantOwnershipError("tenant_id_required")
        actor = str(actor_ref or "").strip()
        if not actor:
            raise QueueError("actor_ref_required")

        with self._lock:
            task = self.get(queue_task_id)
            if str(getattr(task, "tenant_id", "") or "").strip() != tid:
                raise QueueTenantOwnershipError("tenant_mismatch")
            if task.status != STATUS_DEAD_LETTERED:
                raise QueueTransitionError(task.status, STATUS_QUEUED)

            meta = dict(task.metadata or {})
            # Preserve idempotency_key if present; never invent one.
            meta["redriven_by"] = actor
            meta["redriven_at"] = stamp.isoformat()
            meta.pop("last_failure_metadata", None)

            redriven = self._transition(
                task,
                STATUS_QUEUED,
                attempt=int(task.attempt) + 1,
                available_at=stamp,
                updated_at=stamp,
                completed_at=None,
                failed_at=None,
                error_code=None,
                lease_id=None,
                leased_at=None,
                lease_expires_at=None,
                worker_id=None,
                metadata=sanitize_metadata(meta),
            )
            self.store.save(redriven)
            self._obs_emit(
                "queue.redriven",
                redriven,
                status="queued",
                metadata={
                    "actor_ref": actor,
                    "tenant_id": tid,
                    "execution_key": redriven.execution_key,
                    "idempotency_key": meta.get("idempotency_key"),
                },
            )
            return redriven

    def cancel(self, queue_task_id: str, *, now: datetime | None = None) -> QueueTask:
        with self._lock:
            task = self.get(queue_task_id)
            if task.status in TERMINAL_STATUSES:
                return task
            if task.status == STATUS_RUNNING:
                updated = replace(
                    task,
                    updated_at=now or self.now(),
                    metadata=sanitize_metadata(
                        {**dict(task.metadata), "cancellation_requested": True}
                    ),
                )
                self.store.save(updated)
                return updated
            cancelled = self._transition(
                task,
                STATUS_CANCELLED,
                completed_at=now or self.now(),
                updated_at=now or self.now(),
                lease_id=None,
                leased_at=None,
                lease_expires_at=None,
                worker_id=None,
            )
            self.store.save(cancelled)
            self._obs_emit("queue.cancelled", cancelled, status="cancelled")
            return cancelled

    def abort_running(
        self,
        queue_task_id: str,
        lease_id: str,
        *,
        worker_id: str | None = None,
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        with self._lock:
            task = self._require_lease(
                queue_task_id, lease_id, stamp, worker_id=worker_id
            )
            cancelled = self._transition(
                task,
                STATUS_CANCELLED,
                completed_at=stamp,
                updated_at=stamp,
                lease_id=None,
                leased_at=None,
                lease_expires_at=None,
                worker_id=None,
            )
            self.store.save(cancelled)
            return cancelled

    def requeue(
        self,
        queue_task_id: str,
        lease_id: str | None = None,
        *,
        now: datetime | None = None,
        available_at: datetime | None = None,
        reason: str | None = None,
        worker_id: str | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        with self._lock:
            task = self.get(queue_task_id)
            if lease_id is not None:
                task = self._require_lease(
                    queue_task_id, lease_id, stamp, worker_id=worker_id
                )
            extra = dict(task.metadata)
            if reason:
                extra["requeue_reason"] = reason
            queued = self._transition(
                task,
                STATUS_QUEUED,
                available_at=available_at or stamp,
                updated_at=stamp,
                lease_id=None,
                leased_at=None,
                lease_expires_at=None,
                worker_id=None,
                metadata=sanitize_metadata(extra),
            )
            self.store.save(queued)
            return queued

    def defer_waiting_approval(
        self,
        queue_task_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
        worker_id: str | None = None,
    ) -> QueueTask:
        return self.requeue(
            queue_task_id,
            lease_id,
            now=now,
            available_at=DEFERRED_UNTIL,
            reason="workflow_waiting_approval",
            worker_id=worker_id,
        )

    def get_dead_letters(self) -> tuple[QueueTask, ...]:
        return self.store.get_dead_letters()

    def recover_stuck_running(
        self, *, now: datetime | None = None, force: bool = False
    ) -> tuple[str, ...]:
        """Reclaim orphaned RUNNING tasks whose lease has expired (or is missing).

        Multi-process safe: NEVER demotes a RUNNING task with a live (unexpired) lease,
        even when force=True. `force` is retained for API compatibility and only
        emphasizes startup reclaim of *expired* leases.
        """

        stamp = now or self.now()
        reclaim = getattr(self.store, "reclaim_expired_running", None)
        if callable(reclaim):
            recovered = reclaim(now=stamp)
            for qid in recovered:
                try:
                    task = self.get(qid)
                    self._obs_emit(
                        "queue.reclaimed",
                        task,
                        status="retry_wait",
                        error_code="worker_interrupted",
                    )
                except QueueTaskNotFoundError:
                    continue
            return tuple(recovered)

        with self._lock:
            recovered: list[str] = []
            if not hasattr(self.store, "list_all"):
                return ()
            # force is intentionally unused for live leases (see docstring).
            _ = force
            for task in self.store.list_all():
                if task.status != STATUS_RUNNING:
                    continue
                lease_expired = (
                    task.lease_expires_at is None or task.lease_expires_at <= stamp
                )
                if not lease_expired:
                    continue
                waiting = self._transition(
                    task,
                    STATUS_RETRY_WAIT,
                    error_code=task.error_code or "worker_interrupted",
                    failed_at=stamp,
                    available_at=stamp,
                    updated_at=stamp,
                    lease_id=None,
                    leased_at=None,
                    lease_expires_at=None,
                    worker_id=None,
                    metadata=sanitize_metadata(
                        {
                            **dict(task.metadata),
                            "recovered_from_running": True,
                            "recovery_reason": "lease_expired",
                        }
                    ),
                )
                self.store.save(waiting)
                self._obs_emit(
                    "queue.reclaimed",
                    waiting,
                    status="retry_wait",
                    error_code="worker_interrupted",
                )
                try:
                    from runtime.metrics import RUNTIME_COUNTERS

                    RUNTIME_COUNTERS.inc(
                        "reclaim",
                        lane=getattr(waiting, "execution_lane", "") or "",
                    )
                except Exception:
                    pass
                recovered.append(task.queue_task_id)
            return tuple(recovered)

    def _require_lease(
        self,
        queue_task_id: str,
        lease_id: str,
        now: datetime,
        *,
        worker_id: str | None = None,
    ) -> QueueTask:
        task = self.get(queue_task_id)
        if task.lease_id is None or task.lease_id != lease_id:
            raise QueueLeaseError("Invalid lease_id.")
        if worker_id is not None and str(task.worker_id or "") != str(worker_id):
            raise QueueLeaseError("Invalid worker_id.")
        if task.lease_expires_at is not None and task.lease_expires_at <= now:
            raise QueueLeaseError("Lease expired.")
        return task

    def _transition(self, task: QueueTask, target: str, **fields) -> QueueTask:
        if target == task.status and target == STATUS_LEASED:
            return replace(task, **fields)
        allowed = ALLOWED_TRANSITIONS.get(task.status, frozenset())
        if target not in allowed:
            raise QueueTransitionError(task.status, target)
        return replace(task, status=target, **fields)
