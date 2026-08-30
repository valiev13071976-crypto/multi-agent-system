"""Minimal production admission + capacity limits for durable workflows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from task_queue.lanes import (
    LANE_BULK,
    LANE_INTERACTIVE,
    LANE_SCHEDULED,
    is_interactive_lane,
    resolve_execution_lane,
)
from task_queue.models import (
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    STATUS_LEASED,
    STATUS_QUEUED,
    STATUS_RETRY_WAIT,
    STATUS_RUNNING,
    utc_now,
)


DECISION_ACCEPT = "ACCEPT"
DECISION_REJECT = "REJECT"
DECISION_DEFER = "DEFER"

PENDING_STATUSES = frozenset({STATUS_QUEUED, STATUS_RETRY_WAIT})
RUNNING_STATUSES = frozenset({STATUS_LEASED, STATUS_RUNNING})


class AdmissionRejectedError(Exception):
    def __init__(self, reason: str, *, decision: str = DECISION_REJECT):
        self.reason = reason
        self.decision = decision
        super().__init__(reason)


@dataclass(frozen=True)
class AdmissionLimits:
    max_pending_global: int | None = 1000
    max_pending_per_tenant: int | None = 100
    max_running_global: int | None = 100
    max_running_per_tenant: int | None = 20
    max_queue_age_seconds: float | None = None
    interactive_pending_reserve: int = 50
    interactive_reserved_running: int = 5
    max_batch_pending_per_tenant: int | None = None

    @classmethod
    def from_env(cls, env: Mapping | None = None) -> "AdmissionLimits":
        source = env if env is not None else os.environ

        def _int(name: str, default: int | None) -> int | None:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                value = int(str(raw).strip())
            except ValueError:
                return default
            if value <= 0:
                return None  # disabled
            return value

        def _float(name: str, default: float | None) -> float | None:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                value = float(str(raw).strip())
            except ValueError:
                return default
            if value <= 0:
                return None
            return value

        def _int_req(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                return max(0, int(str(raw).strip()))
            except ValueError:
                return default

        return cls(
            max_pending_global=_int("MAX_PENDING_GLOBAL", 1000),
            max_pending_per_tenant=_int("MAX_PENDING_PER_TENANT", 100),
            max_running_global=_int("MAX_RUNNING_GLOBAL", 100),
            max_running_per_tenant=_int("MAX_RUNNING_PER_TENANT", 20),
            max_queue_age_seconds=_float("MAX_QUEUE_AGE_SECONDS", None),
            interactive_pending_reserve=_int_req("INTERACTIVE_PENDING_RESERVE", 50),
            interactive_reserved_running=_int_req("INTERACTIVE_RESERVED", 5),
            max_batch_pending_per_tenant=_int("MAX_BATCH_PENDING_PER_TENANT", None),
        )


@dataclass(frozen=True)
class AdmissionDecision:
    decision: str
    reason_code: str
    priority: str = PRIORITY_NORMAL
    metadata: Mapping[str, object] | None = None


def is_interactive_priority(priority: str) -> bool:
    return priority in {PRIORITY_CRITICAL, PRIORITY_HIGH}


class AdmissionController:
    """Shared/durable-queue-aware admission for Path B create/enqueue."""

    def __init__(self, limits: AdmissionLimits | None = None, *, observability=None):
        self.limits = limits or AdmissionLimits.from_env()
        self.observability = observability

    def _emit(self, decision: AdmissionDecision, *, tenant_id: str = "", lane: str = "") -> None:
        obs = self.observability
        if obs is None:
            return
        from observability.helpers import safe_emit

        meta = {
            "decision": decision.decision,
            "reason_code": decision.reason_code,
            "tenant_id": tenant_id,
            "priority": decision.priority,
            "execution_lane": lane,
            **dict(decision.metadata or {}),
        }
        capacity_reject = decision.decision == DECISION_REJECT and decision.reason_code in {
            "tenant_pending_limit",
            "tenant_quota",
            "global_pending_limit",
            "tenant_batch_pending_limit",
        }
        if capacity_reject:
            meta["backpressure"] = True
        safe_emit(
            obs,
            "workflow.admission",
            context=obs.create_context(workflow_id="", task_id="admission"),
            component="admission",
            status=decision.decision.lower(),
            error_code=decision.reason_code
            if decision.decision != DECISION_ACCEPT
            else None,
            metadata=meta,
        )
        if capacity_reject:
            safe_emit(
                obs,
                "workflow.backpressure",
                context=obs.create_context(workflow_id="", task_id="admission"),
                component="admission",
                status="rejected",
                error_code=decision.reason_code,
                metadata=meta,
            )

    def evaluate_enqueue(
        self,
        queue,
        *,
        tenant_id: str,
        priority: str = PRIORITY_NORMAL,
        execution_lane: str | None = None,
        metadata: Mapping | None = None,
        now: datetime | None = None,
        deadline_at: datetime | None = None,
    ) -> AdmissionDecision:
        stamp = now or utc_now()
        if deadline_at is not None:
            due = deadline_at
            if getattr(due, "tzinfo", None) is None:
                from datetime import timezone

                due = due.replace(tzinfo=timezone.utc)
            check = stamp if stamp.tzinfo is not None else stamp
            if check.tzinfo is None:
                from datetime import timezone

                check = check.replace(tzinfo=timezone.utc)
            if check >= due:
                decision = AdmissionDecision(
                    DECISION_REJECT,
                    "deadline_expired",
                    priority=priority,
                    metadata={"deadline_at": due.isoformat()},
                )
                self._emit(decision, tenant_id=tenant_id, lane="")
                try:
                    from observability.runtime_metrics import RUNTIME_METRICS

                    RUNTIME_METRICS.record_admission(decision.decision, lane="")
                except Exception:
                    pass
                return decision
        lane = resolve_execution_lane(
            execution_lane=execution_lane, priority=priority, metadata=metadata
        )
        counts = count_queue_capacity(queue, tenant_id=tenant_id, now=stamp)
        lim = self.limits
        interactive = is_interactive_lane(lane) or is_interactive_priority(priority)

        if lim.max_pending_per_tenant is not None:
            if counts["pending_tenant"] >= lim.max_pending_per_tenant:
                decision = AdmissionDecision(
                    DECISION_REJECT,
                    "tenant_pending_limit",
                    priority=priority,
                    metadata={
                        "pending_tenant": counts["pending_tenant"],
                        "lane": lane,
                        "reason_alias": "tenant_quota",
                    },
                )
                self._emit(decision, tenant_id=tenant_id, lane=lane)
                try:
                    from observability.runtime_metrics import RUNTIME_METRICS

                    RUNTIME_METRICS.record_admission(decision.decision, lane=lane)
                except Exception:
                    pass
                try:
                    from runtime.metrics import RUNTIME_COUNTERS

                    RUNTIME_COUNTERS.inc("quota_reject", lane=lane)
                except Exception:
                    pass
                return decision

        # Optional per-tenant batch/bulk pending cap (Scale).
        if lim.max_batch_pending_per_tenant is not None and lane in {
            LANE_BULK,
            LANE_SCHEDULED,
        }:
            pending_by_lane = dict(counts.get("pending_by_lane") or {})
            batch_pending = _tenant_batch_pending(queue, tenant_id=tenant_id, now=stamp)
            if batch_pending >= lim.max_batch_pending_per_tenant:
                decision = AdmissionDecision(
                    DECISION_REJECT,
                    "tenant_quota",
                    priority=priority,
                    metadata={
                        "batch_pending_tenant": batch_pending,
                        "lane": lane,
                        "alias_reason": "tenant_batch_pending_limit",
                        "pending_by_lane": pending_by_lane,
                    },
                )
                self._emit(decision, tenant_id=tenant_id, lane=lane)
                try:
                    from observability.runtime_metrics import RUNTIME_METRICS

                    RUNTIME_METRICS.record_admission(decision.decision, lane=lane)
                except Exception:
                    pass
                try:
                    from runtime.metrics import RUNTIME_COUNTERS

                    RUNTIME_COUNTERS.inc("quota_reject", lane=lane)
                except Exception:
                    pass
                return decision

        if lim.max_pending_global is not None:
            if counts["pending_global"] >= lim.max_pending_global:
                pending_by_lane = dict(counts.get("pending_by_lane") or {})
                interactive_pending = int(pending_by_lane.get(LANE_INTERACTIVE, 0))
                if interactive:
                    # Bulk saturation must not close interactive while reserve remains.
                    if interactive_pending < int(lim.interactive_pending_reserve):
                        decision = AdmissionDecision(
                            DECISION_ACCEPT,
                            "accepted_interactive_reserve",
                            priority=priority,
                            metadata={
                                **counts,
                                "lane": lane,
                                "interactive_pending": interactive_pending,
                            },
                        )
                        self._emit(decision, tenant_id=tenant_id, lane=lane)
                        try:
                            from observability.runtime_metrics import RUNTIME_METRICS

                            RUNTIME_METRICS.record_admission(decision.decision, lane=lane)
                        except Exception:
                            pass
                        return decision
                    decision = AdmissionDecision(
                        DECISION_DEFER,
                        "global_pending_saturated_interactive",
                        priority=priority,
                        metadata={"pending_global": counts["pending_global"], "lane": lane},
                    )
                else:
                    decision = AdmissionDecision(
                        DECISION_REJECT,
                        "global_pending_limit",
                        priority=priority,
                        metadata={"pending_global": counts["pending_global"], "lane": lane},
                    )
                self._emit(decision, tenant_id=tenant_id, lane=lane)
                try:
                    from observability.runtime_metrics import RUNTIME_METRICS

                    RUNTIME_METRICS.record_admission(decision.decision, lane=lane)
                except Exception:
                    pass
                if decision.decision == DECISION_REJECT:
                    try:
                        from runtime.metrics import RUNTIME_COUNTERS

                        RUNTIME_COUNTERS.inc("overload_reject", lane=lane)
                    except Exception:
                        pass
                return decision

        decision = AdmissionDecision(
            DECISION_ACCEPT,
            "accepted",
            priority=priority,
            metadata={**counts, "lane": lane},
        )
        self._emit(decision, tenant_id=tenant_id, lane=lane)
        try:
            from observability.runtime_metrics import RUNTIME_METRICS

            RUNTIME_METRICS.record_admission(decision.decision, lane=lane)
        except Exception:
            pass
        return decision

    def require_enqueue(
        self,
        queue,
        *,
        tenant_id: str,
        priority: str = PRIORITY_NORMAL,
        execution_lane: str | None = None,
        metadata: Mapping | None = None,
        now: datetime | None = None,
        deadline_at: datetime | None = None,
    ) -> AdmissionDecision:
        from config.runtime_health import DRAIN

        if DRAIN.draining:
            raise AdmissionRejectedError("api_draining", decision=DECISION_REJECT)
        decision = self.evaluate_enqueue(
            queue,
            tenant_id=tenant_id,
            priority=priority,
            execution_lane=execution_lane,
            metadata=metadata,
            now=now,
            deadline_at=deadline_at,
        )
        if decision.decision == DECISION_ACCEPT:
            return decision
        raise AdmissionRejectedError(decision.reason_code, decision=decision.decision)


def count_queue_capacity(queue, *, tenant_id: str = "", now: datetime | None = None) -> dict:
    """Count pending/running from durable or in-memory queue store."""

    stamp = now or utc_now()
    store = getattr(queue, "store", None)
    counter = getattr(store, "count_by_status", None)
    if callable(counter):
        return counter(tenant_id=tenant_id, now=stamp)

    pending_global = pending_tenant = running_global = running_tenant = 0
    pending_by_lane: dict[str, int] = {}
    running_by_lane: dict[str, int] = {}
    items = []
    if store is not None and hasattr(store, "list_all"):
        items = list(store.list_all())
    for item in items:
        lane = getattr(item, "execution_lane", None) or "background"
        if item.status in PENDING_STATUSES:
            pending_global += 1
            pending_by_lane[lane] = pending_by_lane.get(lane, 0) + 1
            if tenant_id and item.tenant_id == tenant_id:
                pending_tenant += 1
        elif item.status in RUNNING_STATUSES:
            if (
                item.status == STATUS_LEASED
                and item.lease_expires_at is not None
                and item.lease_expires_at <= stamp
            ):
                pending_global += 1
                pending_by_lane[lane] = pending_by_lane.get(lane, 0) + 1
                if tenant_id and item.tenant_id == tenant_id:
                    pending_tenant += 1
                continue
            running_global += 1
            running_by_lane[lane] = running_by_lane.get(lane, 0) + 1
            if tenant_id and item.tenant_id == tenant_id:
                running_tenant += 1
    return {
        "pending_global": pending_global,
        "pending_tenant": pending_tenant,
        "running_global": running_global,
        "running_tenant": running_tenant,
        "pending_by_lane": pending_by_lane,
        "running_by_lane": running_by_lane,
    }


def _tenant_batch_pending(queue, *, tenant_id: str, now: datetime | None = None) -> int:
    stamp = now or utc_now()
    tid = str(tenant_id or "").strip()
    if not tid:
        return 0
    store = getattr(queue, "store", None)
    items = []
    if store is not None and hasattr(store, "list_all"):
        items = list(store.list_all())
    total = 0
    for item in items:
        if str(getattr(item, "tenant_id", "") or "") != tid:
            continue
        if item.status not in PENDING_STATUSES:
            continue
        lane = getattr(item, "execution_lane", None) or "background"
        if lane in {LANE_BULK, LANE_SCHEDULED}:
            total += 1
    return total


def task_past_deadline(task, *, now: datetime | None = None) -> bool:
    stamp = now or utc_now()
    meta = dict(getattr(task, "metadata", None) or {})
    deadline = meta.get("deadline_at") or meta.get("workflow_deadline_at")
    if deadline:
        try:
            text = str(deadline)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            from datetime import datetime as dt

            due = dt.fromisoformat(text)
            if due.tzinfo is None:
                from datetime import timezone

                due = due.replace(tzinfo=timezone.utc)
            if due <= stamp:
                return True
        except Exception:
            pass
    # Interactive lane: honor optional MAX_QUEUE_AGE; skip scheduled/bulk.
    from task_queue.lanes import LANE_SCHEDULED, is_interactive_lane

    lane = getattr(task, "execution_lane", None) or meta.get("execution_lane")
    limits = AdmissionLimits.from_env()
    if limits.max_queue_age_seconds is not None:
        created = getattr(task, "created_at", None)
        if created is not None and (
            stamp - created
        ).total_seconds() > float(limits.max_queue_age_seconds):
            if meta.get("trigger") == "scheduled" or lane == LANE_SCHEDULED:
                return False
            if getattr(task, "priority", "") == PRIORITY_LOW:
                return False
            if is_interactive_lane(str(lane or "")) or is_interactive_priority(
                getattr(task, "priority", PRIORITY_NORMAL)
            ):
                return True
            return False
    return False
