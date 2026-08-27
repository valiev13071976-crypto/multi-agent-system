"""Persisted schedule contracts for one-time / interval workflow launches."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping

from workflow.definition import ScheduleSpec
from workflow.models import utc_now


@dataclass(frozen=True)
class ScheduleState:
    schedule_id: str
    workflow_type: str
    version: str
    payload: Mapping[str, object]
    next_run_at: datetime
    interval_seconds: float | None
    enabled: bool
    last_enqueued_at: datetime | None = None
    last_execution_key: str | None = None
    run_count: int = 0

    def __post_init__(self):
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload or {})))


class ScheduleStore:
    def get(self, schedule_id: str) -> ScheduleState | None:
        raise NotImplementedError

    def save(self, state: ScheduleState) -> None:
        raise NotImplementedError

    def list_due(self, now: datetime) -> tuple[ScheduleState, ...]:
        raise NotImplementedError

    def list_all(self) -> tuple[ScheduleState, ...]:
        raise NotImplementedError


class InMemoryScheduleStore(ScheduleStore):
    def __init__(self):
        self._items: dict[str, ScheduleState] = {}

    def get(self, schedule_id: str) -> ScheduleState | None:
        return self._items.get(schedule_id)

    def save(self, state: ScheduleState) -> None:
        self._items[state.schedule_id] = state

    def list_due(self, now: datetime) -> tuple[ScheduleState, ...]:
        return tuple(
            s
            for s in self._items.values()
            if s.enabled and s.next_run_at <= now
        )

    def list_all(self) -> tuple[ScheduleState, ...]:
        return tuple(self._items.values())


class WorkflowScheduler:
    """Basic scheduler: persist next_run, enqueue via service with idempotent keys."""

    def __init__(self, store: ScheduleStore | None = None, *, now_fn=None):
        self.store = store or InMemoryScheduleStore()
        self._now = now_fn or utc_now

    def register(self, spec: ScheduleSpec) -> ScheduleState:
        now = self._now()
        next_run = spec.run_at or now
        state = ScheduleState(
            schedule_id=spec.schedule_id,
            workflow_type=spec.workflow_type,
            version=spec.version,
            payload=dict(spec.payload),
            next_run_at=next_run,
            interval_seconds=spec.interval_seconds,
            enabled=spec.enabled,
        )
        self.store.save(state)
        return state

    def due(self, now: datetime | None = None) -> tuple[ScheduleState, ...]:
        return self.store.list_due(now or self._now())

    def mark_enqueued(
        self, schedule_id: str, *, execution_key: str, now: datetime | None = None
    ) -> ScheduleState:
        state = self.store.get(schedule_id)
        if state is None:
            raise KeyError(schedule_id)
        stamp = now or self._now()
        next_run = state.next_run_at
        enabled = state.enabled
        if state.interval_seconds:
            next_run = stamp + timedelta(seconds=float(state.interval_seconds))
        else:
            # one-shot
            enabled = False
        state = replace(
            state,
            last_enqueued_at=stamp,
            last_execution_key=execution_key,
            run_count=int(state.run_count) + 1,
            next_run_at=next_run,
            enabled=enabled,
        )
        self.store.save(state)
        return state

    def execution_key_for(self, state: ScheduleState, *, now: datetime | None = None) -> str:
        stamp = now or self._now()
        # Idempotent per schedule fire window (second precision for one-shot / interval tick)
        slot = int(stamp.timestamp())
        if state.interval_seconds:
            slot = int(state.next_run_at.timestamp())
        return f"schedule:{state.schedule_id}:{slot}"
