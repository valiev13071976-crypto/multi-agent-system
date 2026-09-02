"""Scheduled business automation service."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from scheduled_automation.access import (
    PERM_SCHEDULE_CREATE,
    PERM_SCHEDULE_ENABLE,
    PERM_SCHEDULE_READ,
    PERM_SCHEDULE_RUN_NOW,
    PERM_SCHEDULE_UPDATE,
    ScheduleAccessPolicy,
)
from scheduled_automation.clock import Clock
from scheduled_automation.config import (
    MAX_CATCH_UP_OCCURRENCES,
    MIN_INTERVAL_SECONDS,
    WORKFLOW_TYPE_BUSINESS_AUTOMATION,
    scheduled_business_automation_live_active,
)
from scheduled_automation.dispatcher import ScheduledAutomationDispatcher
from scheduled_automation.errors import (
    CAPABILITY_DENIED,
    INVALID_SCHEDULE,
    LIVE_FALLBACK_FORBIDDEN,
    OVERLAP_BLOCKED,
    SCHEDULE_NOT_FOUND,
    STALE_VERSION,
    UNSUPPORTED_TARGET,
    ScheduledAutomationError,
)
from scheduled_automation.models import (
    ALLOWED_TARGETS,
    MISFIRE_RUN_ONCE,
    MISFIRE_SKIP,
    OCC_BLOCKED,
    OCC_DISPATCHED,
    OCC_FAILED,
    OCC_PENDING,
    OCC_SKIPPED,
    OCC_WAITING_APPROVAL,
    OVERLAP_FORBID,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONCE,
    SCHEDULE_TYPES,
    ScheduleDefinition,
    ScheduleOccurrence,
    WORKLOAD_BATCH,
    WORKLOAD_BACKGROUND,
    WORKLOAD_NORMAL,
)
from scheduled_automation.observability import ScheduleObservability
from scheduled_automation.recurrence import compute_next_run, execution_key, misfire_occurrences, occurrence_id, parse_utc
from scheduled_automation.store import InMemoryScheduleAutomationStore, ScheduleAutomationStore
from security.identity import RequestSecurityContext
from security.tenant import require_tenant_id

FORBIDDEN_EXECUTABLE_KEYS = frozenset({"code", "script", "shell", "eval", "sql", "exec"})


class ScheduledAutomationService:
    def __init__(
        self,
        *,
        store: ScheduleAutomationStore | None = None,
        dispatcher: ScheduledAutomationDispatcher | None = None,
        access: ScheduleAccessPolicy | None = None,
        clock: Clock | None = None,
        capability_checker: Callable[[str, tuple[str, ...]], bool] | None = None,
        budget_checker: Callable[[str, dict[str, Any]], bool] | None = None,
        obs: ScheduleObservability | None = None,
    ):
        self._store = store or InMemoryScheduleAutomationStore()
        self._dispatcher = dispatcher or ScheduledAutomationDispatcher()
        self._access = access or ScheduleAccessPolicy()
        self._clock = clock or Clock()
        self._capability_checker = capability_checker or (lambda tenant, caps: True)
        self._budget_checker = budget_checker or (lambda tenant, meta: True)
        self._obs = obs or ScheduleObservability()
        self._running: dict[tuple[str, str], str] = {}

    @property
    def store(self) -> ScheduleAutomationStore:
        return self._store

    @property
    def observability(self) -> ScheduleObservability:
        return self._obs

    def _now(self) -> datetime:
        return self._clock.now()

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def create_schedule(self, ctx: RequestSecurityContext, payload: dict[str, Any]) -> dict[str, Any]:
        tenant = require_tenant_id(str(payload.get("tenant_id") or ctx.tenant_id))
        self._access.require(ctx, PERM_SCHEDULE_CREATE, tenant_id=tenant)
        if scheduled_business_automation_live_active():
            raise ScheduledAutomationError(LIVE_FALLBACK_FORBIDDEN, "live_not_implemented")
        self._validate_payload(payload)
        caps = tuple(payload.get("required_capabilities") or ())
        if not self._capability_checker(tenant, caps):
            raise ScheduledAutomationError(CAPABILITY_DENIED, "capability_denied_at_create")
        now = self._now_iso()
        sid = str(payload.get("schedule_id") or uuid.uuid4())
        start_at = str(payload.get("start_at") or now)
        next_run = compute_next_run(
            schedule_type=str(payload["schedule_type"]),
            timezone_name=str(payload.get("timezone") or "UTC"),
            start_at=start_at,
            end_at=payload.get("end_at"),
            interval_seconds=payload.get("interval_seconds"),
            daily_time=payload.get("daily_time"),
            weekly_day=payload.get("weekly_day"),
            from_time=self._now(),
            occurrence_count=0,
            max_occurrences=payload.get("max_occurrences"),
        )
        definition = ScheduleDefinition(
            schedule_id=sid,
            tenant_id=tenant,
            owner_id=str(payload.get("owner_id") or ctx.user_id or "owner"),
            name=str(payload.get("name") or "automation"),
            enabled=bool(payload.get("enabled", True)),
            paused=False,
            schedule_type=str(payload["schedule_type"]),
            timezone=str(payload.get("timezone") or "UTC"),
            start_at=start_at,
            end_at=payload.get("end_at"),
            interval_seconds=int(payload["interval_seconds"]) if payload.get("interval_seconds") is not None else None,
            daily_time=payload.get("daily_time"),
            weekly_day=payload.get("weekly_day"),
            max_occurrences=payload.get("max_occurrences"),
            misfire_policy=str(payload.get("misfire_policy") or MISFIRE_SKIP),
            overlap_policy=str(payload.get("overlap_policy") or OVERLAP_FORBID),
            workload_class=str(payload.get("workload_class") or WORKLOAD_BACKGROUND),
            priority=min(5, max(1, int(payload.get("priority") or 3))),
            target_type=str(payload["target_type"]),
            target_payload=dict(payload.get("target_payload") or {}),
            required_capabilities=caps,
            version=1,
            occurrence_count=0,
            failure_count=0,
            next_run_at=next_run.isoformat() if next_run else None,
            last_dispatch_at=None,
            last_run_id=None,
            created_at=now,
            updated_at=now,
            created_by=str(ctx.user_id or "system"),
            metadata=dict(payload.get("metadata") or {}),
        )
        self._store.create_schedule(definition)
        self._store.append_audit(tenant_id=tenant, event_type="schedule_created", schedule_id=sid, payload={"version": 1})
        return self._schedule_dict(definition)

    def get_schedule(self, ctx: RequestSecurityContext, *, tenant_id: str, schedule_id: str) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_SCHEDULE_READ, tenant_id=tenant)
        s = self._store.get_schedule(tenant_id=tenant, schedule_id=schedule_id)
        if s is None:
            raise ScheduledAutomationError(SCHEDULE_NOT_FOUND, schedule_id)
        return self._schedule_dict(s)

    def list_schedules(self, ctx: RequestSecurityContext, *, tenant_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_SCHEDULE_READ, tenant_id=tenant)
        items = [self._schedule_dict(s) for s in self._store.list_schedules(tenant_id=tenant, limit=limit, offset=offset)]
        return {"tenant_id": tenant, "items": items, "limit": limit, "offset": offset, "mode": "FIXTURE"}

    def update_schedule(self, ctx: RequestSecurityContext, *, tenant_id: str, schedule_id: str, patch: dict[str, Any], expected_version: int) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_SCHEDULE_UPDATE, tenant_id=tenant)
        current = self._store.get_schedule(tenant_id=tenant, schedule_id=schedule_id)
        if current is None:
            raise ScheduledAutomationError(SCHEDULE_NOT_FOUND, schedule_id)
        if current.version != expected_version:
            raise ScheduledAutomationError(STALE_VERSION, "version_mismatch")
        merged = {**current.__dict__, **patch, "version": current.version + 1, "updated_at": self._now_iso()}
        if "target_payload" in patch:
            merged["target_payload"] = dict(patch["target_payload"])
        self._validate_payload(merged, partial=True)
        next_run = compute_next_run(
            schedule_type=merged["schedule_type"],
            timezone_name=merged["timezone"],
            start_at=merged["start_at"],
            end_at=merged.get("end_at"),
            interval_seconds=merged.get("interval_seconds"),
            daily_time=merged.get("daily_time"),
            weekly_day=merged.get("weekly_day"),
            from_time=self._now(),
            occurrence_count=merged["occurrence_count"],
            max_occurrences=merged.get("max_occurrences"),
        )
        merged["next_run_at"] = next_run.isoformat() if next_run else None
        updated = ScheduleDefinition(**merged)
        try:
            self._store.update_schedule(updated, expected_version=expected_version)
        except ValueError as exc:
            raise ScheduledAutomationError(STALE_VERSION, str(exc)) from exc
        self._store.append_audit(tenant_id=tenant, event_type="schedule_updated", schedule_id=schedule_id, payload={"version": updated.version})
        return self._schedule_dict(updated)

    def set_enabled(self, ctx: RequestSecurityContext, *, tenant_id: str, schedule_id: str, enabled: bool) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_SCHEDULE_ENABLE, tenant_id=tenant)
        current = self._store.get_schedule(tenant_id=tenant, schedule_id=schedule_id)
        if current is None:
            raise ScheduledAutomationError(SCHEDULE_NOT_FOUND, schedule_id)
        updated = replace(current, enabled=enabled, updated_at=self._now_iso())
        self._store.update_schedule(updated, expected_version=current.version)
        self._store.append_audit(tenant_id=tenant, event_type="enabled" if enabled else "disabled", schedule_id=schedule_id, payload={})
        return self._schedule_dict(updated)

    def pause(self, ctx: RequestSecurityContext, *, tenant_id: str, schedule_id: str) -> dict[str, Any]:
        return self._set_paused(ctx, tenant_id=tenant_id, schedule_id=schedule_id, paused=True)

    def resume(self, ctx: RequestSecurityContext, *, tenant_id: str, schedule_id: str) -> dict[str, Any]:
        return self._set_paused(ctx, tenant_id=tenant_id, schedule_id=schedule_id, paused=False)

    def _set_paused(self, ctx: RequestSecurityContext, *, tenant_id: str, schedule_id: str, paused: bool) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_SCHEDULE_ENABLE, tenant_id=tenant)
        current = self._store.get_schedule(tenant_id=tenant, schedule_id=schedule_id)
        if current is None:
            raise ScheduledAutomationError(SCHEDULE_NOT_FOUND, schedule_id)
        updated = replace(current, paused=paused, updated_at=self._now_iso())
        self._store.update_schedule(updated, expected_version=current.version)
        self._store.append_audit(tenant_id=tenant, event_type="paused" if paused else "resumed", schedule_id=schedule_id, payload={})
        return self._schedule_dict(updated)

    def run_now(self, ctx: RequestSecurityContext, *, tenant_id: str, schedule_id: str) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_SCHEDULE_RUN_NOW, tenant_id=tenant)
        schedule = self._store.get_schedule(tenant_id=tenant, schedule_id=schedule_id)
        if schedule is None:
            raise ScheduledAutomationError(SCHEDULE_NOT_FOUND, schedule_id)
        if not self._capability_checker(tenant, schedule.required_capabilities):
            raise ScheduledAutomationError(CAPABILITY_DENIED, "capability_denied")
        occ = self._materialize_occurrence(schedule, scheduled_for=self._now(), manual=True)
        result = self._dispatch_occurrence(schedule, occ)
        self._store.append_audit(tenant_id=tenant, event_type="run_now", schedule_id=schedule_id, payload={"occurrence_id": occ.occurrence_id})
        return {"occurrence": occ.__dict__, "dispatch": result.__dict__, "mutation": False}

    def list_runs(self, ctx: RequestSecurityContext, *, tenant_id: str, schedule_id: str, limit: int = 50) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_SCHEDULE_READ, tenant_id=tenant)
        occs = self._store.list_occurrences(tenant_id=tenant, schedule_id=schedule_id, limit=limit)
        return {"tenant_id": tenant, "schedule_id": schedule_id, "runs": [o.__dict__ for o in occs]}

    def tick(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        now = self._now()
        now_iso = now.isoformat()
        results = []
        for schedule in self._store.list_due_schedules(tenant_id=tenant_id, now=now_iso):
            if schedule.overlap_policy == OVERLAP_FORBID and self._running.get((schedule.tenant_id, schedule.schedule_id)):
                self._obs.emit(event="overlap_blocked", schedule_id=schedule.schedule_id, tenant_id=schedule.tenant_id)
                continue
            if not self._capability_checker(schedule.tenant_id, schedule.required_capabilities):
                occ = self._materialize_occurrence(schedule, scheduled_for=parse_utc(schedule.next_run_at or now_iso))
                occ = ScheduleOccurrence(**{**occ.__dict__, "status": OCC_BLOCKED, "error_code": CAPABILITY_DENIED})
                self._store.save_occurrence(occ)
                self._obs.emit(event="blocked_by_auth", schedule_id=schedule.schedule_id)
                results.append({"schedule_id": schedule.schedule_id, "status": OCC_BLOCKED})
                self._advance_schedule(schedule, now)
                continue
            if not self._budget_checker(schedule.tenant_id, {"schedule_id": schedule.schedule_id}):
                occ = self._materialize_occurrence(schedule, scheduled_for=parse_utc(schedule.next_run_at or now_iso))
                occ = ScheduleOccurrence(**{**occ.__dict__, "status": OCC_BLOCKED, "error_code": "BUDGET_DENIED"})
                self._store.save_occurrence(occ)
                results.append({"schedule_id": schedule.schedule_id, "status": OCC_BLOCKED})
                self._advance_schedule(schedule, now)
                continue
            scheduled_for = parse_utc(schedule.next_run_at or now_iso)
            lag_seconds = max(0.0, (now - scheduled_for).total_seconds())
            grace_seconds = float(schedule.interval_seconds or MIN_INTERVAL_SECONDS)
            if lag_seconds > grace_seconds and schedule.misfire_policy == MISFIRE_SKIP:
                occ = self._materialize_occurrence(schedule, scheduled_for=scheduled_for)
                occ = ScheduleOccurrence(**{**occ.__dict__, "status": OCC_SKIPPED})
                self._store.save_occurrence(occ)
                self._obs.emit(event="misfire_skip", schedule_id=schedule.schedule_id)
                results.append({"schedule_id": schedule.schedule_id, "status": OCC_SKIPPED})
                self._advance_schedule(schedule, now)
                continue
            if lag_seconds > grace_seconds and schedule.misfire_policy == MISFIRE_RUN_ONCE:
                scheduled_for = now
            if lag_seconds > grace_seconds:
                fires = misfire_occurrences(
                    policy=schedule.misfire_policy,
                    due_at=scheduled_for,
                    now=now,
                    max_catch_up=MAX_CATCH_UP_OCCURRENCES,
                )
                if not fires:
                    occ = self._materialize_occurrence(schedule, scheduled_for=scheduled_for)
                    occ = ScheduleOccurrence(**{**occ.__dict__, "status": OCC_SKIPPED})
                    self._store.save_occurrence(occ)
                    self._obs.emit(event="misfire_skip", schedule_id=schedule.schedule_id)
                    results.append({"schedule_id": schedule.schedule_id, "status": OCC_SKIPPED})
                    self._advance_schedule(schedule, now)
                    continue
                scheduled_for = fires[0]
            occ = self._materialize_occurrence(schedule, scheduled_for=scheduled_for)
            existing = self._store.get_occurrence(tenant_id=schedule.tenant_id, occurrence_id=occ.occurrence_id)
            if existing and existing.status in {OCC_DISPATCHED, OCC_WAITING_APPROVAL}:
                self._obs.emit(event="duplicate_prevented", occurrence_id=occ.occurrence_id)
                results.append({"schedule_id": schedule.schedule_id, "status": "idempotent"})
                self._advance_schedule(schedule, now)
                continue
            if existing is None:
                self._store.save_occurrence(occ)
            elif existing.status == OCC_PENDING:
                occ = existing
            else:
                results.append({"schedule_id": schedule.schedule_id, "status": existing.status})
                self._advance_schedule(schedule, now)
                continue
            if not self._store.claim_occurrence(tenant_id=schedule.tenant_id, occurrence_id=occ.occurrence_id, now=now_iso):
                results.append({"schedule_id": schedule.schedule_id, "status": "claim_failed"})
                continue
            if schedule.target_payload.get("requires_approval"):
                occ = ScheduleOccurrence(**{**occ.__dict__, "status": OCC_WAITING_APPROVAL})
                self._store.save_occurrence(occ)
                self._obs.emit(event="waiting_approval", occurrence_id=occ.occurrence_id)
                results.append({"schedule_id": schedule.schedule_id, "status": OCC_WAITING_APPROVAL})
                self._advance_schedule(schedule, now)
                continue
            dispatch = self._dispatch_occurrence(schedule, occ)
            results.append({"schedule_id": schedule.schedule_id, "status": dispatch.status, "run_id": dispatch.run_id})
            schedule = self._store.get_schedule(tenant_id=schedule.tenant_id, schedule_id=schedule.schedule_id) or schedule
            self._advance_schedule(schedule, now)
        return results

    def analytics_snapshot(self, *, tenant_id: str) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        schedules = self._store.list_schedules(tenant_id=tenant, limit=1000)
        enabled = [s for s in schedules if s.enabled and not s.paused]
        due = self._store.list_due_schedules(tenant_id=tenant, now=self._now_iso())
        return {
            "tenant_id": tenant,
            "total_schedules": len(schedules),
            "enabled_schedules": len(enabled),
            "paused_schedules": len([s for s in schedules if s.paused]),
            "due_soon": len(due),
            **self._obs.metrics_snapshot(schedules_total=len(schedules), schedules_enabled=len(enabled), due=len(due)),
            "mode": "FIXTURE",
        }

    def ba_create_proposal(self, *, tenant_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "proposal": {
                "name": intent.get("name") or "Scheduled task",
                "schedule_type": intent.get("schedule_type") or SCHEDULE_DAILY,
                "timezone": intent.get("timezone") or "UTC",
                "daily_time": intent.get("daily_time") or "09:00",
                "target_type": intent.get("target_type") or "ANALYTICS_QUERY",
                "target_payload": intent.get("target_payload") or {"question_type": "sales_week"},
                "required_capabilities": ("schedule.create",),
            },
            "mutation": False,
            "mode": "FIXTURE",
        }

    def _dispatch_occurrence(self, schedule: ScheduleDefinition, occ: ScheduleOccurrence) -> Any:
        self._running[(schedule.tenant_id, schedule.schedule_id)] = occ.occurrence_id
        try:
            payload = {
                "target_type": schedule.target_type,
                "target_payload": schedule.target_payload,
                "workload_class": schedule.workload_class,
                "required_capabilities": schedule.required_capabilities,
            }
            result = self._dispatcher.dispatch(tenant_id=schedule.tenant_id, occurrence=occ, schedule_payload=payload)
            occ = ScheduleOccurrence(**{**occ.__dict__, "status": OCC_DISPATCHED, "dispatched_at": self._now_iso(), "run_id": result.run_id})
            self._store.save_occurrence(occ)
            updated = replace(
                schedule,
                occurrence_count=schedule.occurrence_count + 1,
                last_dispatch_at=self._now_iso(),
                last_run_id=result.run_id,
                updated_at=self._now_iso(),
            )
            self._store.update_schedule(updated, expected_version=schedule.version)
            self._obs.emit(event="dispatch_success", schedule_id=schedule.schedule_id, occurrence_id=occ.occurrence_id)
            return result
        finally:
            self._running.pop((schedule.tenant_id, schedule.schedule_id), None)

    def _advance_schedule(self, schedule: ScheduleDefinition, now: datetime) -> None:
        if schedule.schedule_type == SCHEDULE_ONCE:
            updated = replace(schedule, enabled=False, next_run_at=None, updated_at=self._now_iso())
            self._store.update_schedule(updated, expected_version=schedule.version)
            return
        next_run = compute_next_run(
            schedule_type=schedule.schedule_type,
            timezone_name=schedule.timezone,
            start_at=schedule.start_at,
            end_at=schedule.end_at,
            interval_seconds=schedule.interval_seconds,
            daily_time=schedule.daily_time,
            weekly_day=schedule.weekly_day,
            from_time=now,
            occurrence_count=schedule.occurrence_count + 1,
            max_occurrences=schedule.max_occurrences,
        )
        updated = replace(schedule, next_run_at=next_run.isoformat() if next_run else None, enabled=bool(next_run), updated_at=self._now_iso())
        self._store.update_schedule(updated, expected_version=schedule.version)

    def _materialize_occurrence(self, schedule: ScheduleDefinition, *, scheduled_for: datetime, manual: bool = False) -> ScheduleOccurrence:
        oid = occurrence_id(schedule.schedule_id, schedule.version, scheduled_for)
        if manual:
            oid = f"{oid}:manual:{uuid.uuid4().hex[:8]}"
        return ScheduleOccurrence(
            occurrence_id=oid,
            schedule_id=schedule.schedule_id,
            tenant_id=schedule.tenant_id,
            schedule_version=schedule.version,
            scheduled_for=scheduled_for.isoformat(),
            status=OCC_PENDING,
            execution_key=execution_key(schedule.schedule_id, schedule.version, scheduled_for),
            manual=manual,
        )

    def _validate_payload(self, payload: dict[str, Any], *, partial: bool = False) -> None:
        st = payload.get("schedule_type")
        if st and st not in SCHEDULE_TYPES:
            raise ScheduledAutomationError(INVALID_SCHEDULE, "schedule_type")
        if st == SCHEDULE_INTERVAL:
            iv = int(payload.get("interval_seconds") or 0)
            if iv < MIN_INTERVAL_SECONDS:
                raise ScheduledAutomationError(INVALID_SCHEDULE, "interval_too_small")
        target = payload.get("target_type")
        if target and target not in ALLOWED_TARGETS:
            raise ScheduledAutomationError(UNSUPPORTED_TARGET, str(target))
        tp = dict(payload.get("target_payload") or {})
        for key in tp:
            if key.lower() in FORBIDDEN_EXECUTABLE_KEYS:
                raise ScheduledAutomationError(UNSUPPORTED_TARGET, key)
        if payload.get("workload_class") and payload["workload_class"] not in {WORKLOAD_NORMAL, WORKLOAD_BACKGROUND, WORKLOAD_BATCH}:
            raise ScheduledAutomationError(INVALID_SCHEDULE, "workload_class")

    @staticmethod
    def _schedule_dict(s: ScheduleDefinition) -> dict[str, Any]:
        d = dict(s.__dict__)
        d["mode"] = "FIXTURE"
        d["live"] = False
        return d
