"""Durable recovery queue — no side-effect mutation payloads."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta

from recovery.models import (
    ACTION_RECONCILE_READ_ONLY,
    QUEUE_CANCELLED,
    QUEUE_COMPLETED,
    QUEUE_DEAD,
    QUEUE_DEFERRED,
    QUEUE_LEASED,
    QUEUE_PENDING,
    RecoveryQueueJob,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_NORMAL,
    utc_now,
)
from recovery.store import RecoveryConflictError, RecoveryPersistenceUnavailableError


_PRIORITY_ORDER = {
    SEVERITY_CRITICAL: 0,
    SEVERITY_HIGH: 1,
    SEVERITY_NORMAL: 2,
    SEVERITY_LOW: 3,
}


class RecoveryQueue:
    """In-memory queue with optional SQLite-backed store sync."""

    def __init__(self, store=None, *, max_attempts: int = 3):
        self._lock = threading.RLock()
        self._jobs: dict[str, RecoveryQueueJob] = {}
        self.store = store
        self.max_attempts = int(max_attempts)
        self.available = True
        if store is not None and hasattr(store, "list_queue_jobs"):
            for job in store.list_queue_jobs():
                self._jobs[job.job_id] = job

    def enqueue(
        self,
        *,
        recovery_id: str,
        action_type: str = ACTION_RECONCILE_READ_ONLY,
        scheduled_at: datetime | None = None,
        priority: str = SEVERITY_NORMAL,
        attempt: int = 0,
        metadata_safe: dict | None = None,
    ) -> RecoveryQueueJob:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        stamp = scheduled_at or utc_now()
        pri = priority if priority in _PRIORITY_ORDER else SEVERITY_NORMAL
        job = RecoveryQueueJob(
            job_id=str(uuid.uuid4()),
            recovery_id=recovery_id,
            action_type=action_type,
            scheduled_at=stamp,
            priority=pri,
            attempt=int(attempt),
            status=QUEUE_PENDING,
            metadata_safe=metadata_safe or {},
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._persist(job)
        return job

    def get_due_jobs(self, now: datetime | None = None) -> tuple[RecoveryQueueJob, ...]:
        stamp = now or utc_now()
        with self._lock:
            due = [
                j
                for j in self._jobs.values()
                if j.status in {QUEUE_PENDING, QUEUE_DEFERRED} and j.scheduled_at <= stamp
            ]
        due.sort(
            key=lambda j: (
                _PRIORITY_ORDER.get(j.priority, 9),
                j.scheduled_at.isoformat(),
                j.job_id,
            )
        )
        return tuple(due)

    def lease(self, job_id: str, *, now: datetime | None = None) -> RecoveryQueueJob:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        stamp = now or utc_now()
        with self._lock:
            job = self._require(job_id)
            if job.status not in {QUEUE_PENDING, QUEUE_DEFERRED}:
                raise RecoveryConflictError("queue_job_not_leasable")
            updated = self._copy(job, status=QUEUE_LEASED, leased_at=stamp, version=job.version + 1)
            self._jobs[job_id] = updated
            self._persist(updated)
            return updated

    def complete(self, job_id: str, *, now: datetime | None = None) -> RecoveryQueueJob:
        stamp = now or utc_now()
        with self._lock:
            job = self._require(job_id)
            updated = self._copy(
                job,
                status=QUEUE_COMPLETED,
                completed_at=stamp,
                version=job.version + 1,
            )
            self._jobs[job_id] = updated
            self._persist(updated)
            return updated

    def defer(
        self,
        job_id: str,
        *,
        delay_seconds: float,
        now: datetime | None = None,
    ) -> RecoveryQueueJob:
        stamp = now or utc_now()
        with self._lock:
            job = self._require(job_id)
            nxt_attempt = job.attempt + 1
            if nxt_attempt >= self.max_attempts:
                updated = self._copy(
                    job,
                    status=QUEUE_DEAD,
                    attempt=nxt_attempt,
                    completed_at=stamp,
                    metadata_safe={**dict(job.metadata_safe), "dead_letter": True},
                    version=job.version + 1,
                )
            else:
                updated = self._copy(
                    job,
                    status=QUEUE_DEFERRED,
                    attempt=nxt_attempt,
                    scheduled_at=stamp + timedelta(seconds=float(delay_seconds)),
                    leased_at=None,
                    completed_at=None,
                    version=job.version + 1,
                )
            self._jobs[job_id] = updated
            self._persist(updated)
            return updated

    def cancel(self, job_id: str, *, now: datetime | None = None) -> RecoveryQueueJob:
        stamp = now or utc_now()
        with self._lock:
            job = self._require(job_id)
            updated = self._copy(
                job,
                status=QUEUE_CANCELLED,
                completed_at=stamp,
                version=job.version + 1,
            )
            self._jobs[job_id] = updated
            self._persist(updated)
            return updated

    def list_jobs(self) -> tuple[RecoveryQueueJob, ...]:
        with self._lock:
            return tuple(sorted(self._jobs.values(), key=lambda j: j.job_id))

    def _require(self, job_id: str) -> RecoveryQueueJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise RecoveryConflictError("queue_job_not_found")
        return job

    def _persist(self, job: RecoveryQueueJob) -> None:
        store = self.store
        if store is None or not hasattr(store, "save_queue_job"):
            return
        try:
            store.save_queue_job(job)
        except RecoveryPersistenceUnavailableError:
            self.available = False
            raise

    @staticmethod
    def _copy(
        job: RecoveryQueueJob,
        *,
        status: str | None = None,
        attempt: int | None = None,
        scheduled_at: datetime | None = None,
        leased_at=...,
        completed_at=...,
        metadata_safe: dict | None = None,
        version: int | None = None,
    ) -> RecoveryQueueJob:
        return RecoveryQueueJob(
            job_id=job.job_id,
            recovery_id=job.recovery_id,
            action_type=job.action_type,
            scheduled_at=job.scheduled_at if scheduled_at is None else scheduled_at,
            priority=job.priority,
            attempt=job.attempt if attempt is None else attempt,
            status=job.status if status is None else status,
            leased_at=job.leased_at if leased_at is ... else leased_at,
            completed_at=job.completed_at if completed_at is ... else completed_at,
            metadata_safe=metadata_safe if metadata_safe is not None else dict(job.metadata_safe),
            version=job.version if version is None else version,
        )
