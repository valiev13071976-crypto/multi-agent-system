import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Mapping

from security.redaction import redact
from task_queue.errors import (
    QueueDuplicateExecutionError,
    QueueLeaseError,
    QueueTaskNotFoundError,
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
    ):
        self.store = store or InMemoryTaskQueueStore()
        self.retry_policy = retry_policy or RetryPolicy()
        self.lease_seconds = float(lease_seconds)
        self._now = now_fn or utc_now

    def now(self) -> datetime:
        return self._now()

    def get(self, queue_task_id: str) -> QueueTask:
        task = self.store.get(queue_task_id)
        if task is None:
            raise QueueTaskNotFoundError(queue_task_id)
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
    ) -> QueueTask:
        existing = self.store.find_by_execution_key(execution_key)
        active = [item for item in existing if item.status in ACTIVE_STATUSES]
        if active:
            return sorted(active, key=lambda item: (item.created_at, item.queue_task_id))[0]
        terminal = [item for item in existing if item.status in TERMINAL_STATUSES]
        if terminal:
            raise QueueDuplicateExecutionError(execution_key)
        stamp = now or self.now()
        task = QueueTask(
            queue_task_id=queue_task_id or str(uuid.uuid4()),
            workflow_id=workflow_id,
            task_id=task_id,
            execution_key=execution_key,
            status=STATUS_QUEUED,
            priority=priority,
            attempt=0,
            max_attempts=int(
                self.retry_policy.max_attempts if max_attempts is None else max_attempts
            ),
            created_at=stamp,
            available_at=stamp,
            timeout_seconds=timeout_seconds,
            metadata=sanitize_metadata(metadata),
        )
        self.store.enqueue(task)
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
    ) -> QueueTask | None:
        stamp = now or self.now()
        ready = self.list_ready(now=stamp)
        if not ready:
            return None
        task = ready[0]
        ttl = self.lease_seconds if lease_seconds is None else float(lease_seconds)
        lease_fields = {
            "lease_id": str(uuid.uuid4()),
            "leased_at": stamp,
            "lease_expires_at": stamp + timedelta(seconds=ttl),
            "metadata": sanitize_metadata(
                {**dict(task.metadata), "worker_id": worker_id}
            ),
        }
        if task.status == STATUS_LEASED:
            leased = replace(task, **lease_fields)
        else:
            leased = self._transition(task, STATUS_LEASED, **lease_fields)
        self.store.save(leased)
        return leased

    def start(self, queue_task_id: str, lease_id: str, *, now: datetime | None = None) -> QueueTask:
        stamp = now or self.now()
        task = self._require_lease(queue_task_id, lease_id, stamp)
        started = self._transition(
            task,
            STATUS_RUNNING,
            attempt=task.attempt + 1,
            started_at=stamp,
        )
        self.store.save(started)
        return started

    def ack(self, queue_task_id: str, lease_id: str, *, now: datetime | None = None) -> QueueTask:
        stamp = now or self.now()
        task = self._require_lease(queue_task_id, lease_id, stamp)
        done = self._transition(
            task,
            STATUS_COMPLETED,
            completed_at=stamp,
            lease_id=None,
            leased_at=None,
            lease_expires_at=None,
        )
        self.store.save(done)
        return done

    def skip_complete(
        self,
        queue_task_id: str,
        lease_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        task = self._require_lease(queue_task_id, lease_id, stamp)
        done = self._transition(
            task,
            STATUS_COMPLETED,
            completed_at=stamp,
            lease_id=None,
            leased_at=None,
            lease_expires_at=None,
            metadata=sanitize_metadata({**dict(task.metadata), "skipped_reason": reason}),
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
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        task = self._require_lease(queue_task_id, lease_id, stamp)
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
                lease_id=None,
                leased_at=None,
                lease_expires_at=None,
                metadata=sanitize_metadata({**dict(task.metadata), **extra}),
            )
            self.store.save(waiting)
            return waiting
        return self.dead_letter(
            queue_task_id,
            lease_id,
            error_code=code,
            metadata=extra,
            now=stamp,
        )

    def dead_letter(
        self,
        queue_task_id: str,
        lease_id: str | None = None,
        *,
        error_code: str,
        metadata: Mapping | None = None,
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        task = self.get(queue_task_id)
        if lease_id is not None:
            task = self._require_lease(queue_task_id, lease_id, stamp)
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
            lease_id=None,
            leased_at=None,
            lease_expires_at=None,
            metadata=sanitize_metadata({**dict(task.metadata), **extra}),
        )
        self.store.save(lettered)
        return lettered

    def cancel(self, queue_task_id: str, *, now: datetime | None = None) -> QueueTask:
        task = self.get(queue_task_id)
        if task.status in TERMINAL_STATUSES:
            return task
        if task.status == STATUS_RUNNING:
            updated = replace(
                task,
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
            lease_id=None,
            leased_at=None,
            lease_expires_at=None,
        )
        self.store.save(cancelled)
        return cancelled

    def abort_running(
        self,
        queue_task_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> QueueTask:
        stamp = now or self.now()
        task = self._require_lease(queue_task_id, lease_id, stamp)
        cancelled = self._transition(
            task,
            STATUS_CANCELLED,
            completed_at=stamp,
            lease_id=None,
            leased_at=None,
            lease_expires_at=None,
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
    ) -> QueueTask:
        stamp = now or self.now()
        task = self.get(queue_task_id)
        if lease_id is not None:
            task = self._require_lease(queue_task_id, lease_id, stamp)
        extra = dict(task.metadata)
        if reason:
            extra["requeue_reason"] = reason
        queued = self._transition(
            task,
            STATUS_QUEUED,
            available_at=available_at or stamp,
            lease_id=None,
            leased_at=None,
            lease_expires_at=None,
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
    ) -> QueueTask:
        return self.requeue(
            queue_task_id,
            lease_id,
            now=now,
            available_at=DEFERRED_UNTIL,
            reason="workflow_waiting_approval",
        )

    def get_dead_letters(self) -> tuple[QueueTask, ...]:
        return self.store.get_dead_letters()

    def _require_lease(self, queue_task_id: str, lease_id: str, now: datetime) -> QueueTask:
        task = self.get(queue_task_id)
        if task.lease_id is None or task.lease_id != lease_id:
            raise QueueLeaseError("Invalid lease_id.")
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
