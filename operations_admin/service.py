"""Operations admin service — read models and governed commands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from operations_admin.access import AdminAuthorizationPolicy
from operations_admin.alerts import AlertEngine
from operations_admin.audit_store import AdminAuditStore
from operations_admin.capabilities import (
    PERM_OPS_APPROVAL,
    PERM_OPS_COST_READ,
    PERM_OPS_READ,
    PERM_OPS_RECOVERY,
    PERM_OPS_ROUTING_WRITE,
    PERM_OPS_SECURITY_READ,
    PERM_OPS_TENANT_READ,
    PERM_OPS_WRITE,
)
from operations_admin.commands import (
    ActivateRoutingCommand,
    ApprovalDecisionCommand,
    CancelRunCommand,
    RedriveDLQCommand,
    RollbackRoutingCommand,
    confirmation_token,
)
from operations_admin.errors import (
    ADMIN_ACTION_NOT_ALLOWED,
    ADMIN_AUDIT_FAILED,
    ADMIN_CONFIRMATION_INVALID,
    ADMIN_REDRIVE_NOT_ALLOWED,
    ADMIN_STALE_STATE,
    ADMIN_TARGET_NOT_FOUND,
    AdminError,
)
from operations_admin.models import (
    ApprovalQueueView,
    AuditEventView,
    BudgetStatusView,
    DLQItemView,
    FailureView,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    HEALTH_UNHEALTHY,
    OperationsDashboardView,
    QueueView,
    ProviderHealthView,
    RoutingHealthView,
    SideEffectView,
    SystemHealthView,
    TenantUsageView,
    ToolHealthView,
    UsageSummaryView,
    WorkerPoolView,
    WorkflowRunDetail,
    WorkflowRunView,
)
from operations_admin.observability import AdminObservability
from security.identity import RequestSecurityContext
from security.redaction import redact


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _window_bounds(window: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    key = str(window or "24h").lower()
    if key in {"15m", "15min"}:
        return now - timedelta(minutes=15), now
    if key in {"1h", "hour"}:
        return now - timedelta(hours=1), now
    if key in {"7d", "week"}:
        return now - timedelta(days=7), now
    return now - timedelta(hours=24), now


def _map_provider_health(state: str) -> str:
    st = str(state or "").lower()
    if st == "healthy":
        return HEALTH_HEALTHY
    if st in {"degraded", "cooldown"}:
        return HEALTH_DEGRADED
    if st in {"unhealthy", "failed"}:
        return HEALTH_UNHEALTHY
    return HEALTH_UNKNOWN


class OperationsAdminService:
    def __init__(
        self,
        *,
        access: AdminAuthorizationPolicy | None = None,
        audit_store: AdminAuditStore | None = None,
        alert_engine: AlertEngine | None = None,
        side_effect_runtime=None,
        workflow_runtime=None,
        routing_activation=None,
        finops=None,
        budget_guard=None,
        approval_store=None,
        execution_store=None,
        hitl_service=None,
        health_tracker=None,
        saas_store=None,
        obs: AdminObservability | None = None,
        production_foundation=None,
        production_integrations=None,
        controlled_launch=None,
        production_activation=None,
        require_audit: bool = True,
    ):
        self.access = access or AdminAuthorizationPolicy()
        self.audit = audit_store
        self.alerts = alert_engine or AlertEngine()
        self.side_effect_runtime = side_effect_runtime
        self.workflow_runtime = workflow_runtime
        self.routing_activation = routing_activation
        self.finops = finops
        self.budget_guard = budget_guard
        self.approval_store = approval_store
        self.execution_store = execution_store
        self.hitl_service = hitl_service
        self.health_tracker = health_tracker
        self.saas_store = saas_store
        self.production_foundation = production_foundation
        self.production_integrations = production_integrations
        self.controlled_launch = controlled_launch
        self.production_activation = production_activation
        self.obs = obs or AdminObservability()
        self.require_audit = require_audit
        self._processed_idempotency: set[str] = set()
        self._routing_candidates: dict[str, object] = {}

    def register_routing_candidate(self, candidate) -> None:
        cid = str(getattr(candidate, "candidate_id", "") or "")
        if cid:
            self._routing_candidates[cid] = candidate

    def get_routing_candidate(self, candidate_id: str):
        return self._routing_candidates.get(candidate_id)

    def _audit(self, ctx, *, capability, action, target_type, target_id, result, reason=None, tenant_scope=None):
        if self.audit is None:
            if self.require_audit:
                raise AdminError(ADMIN_AUDIT_FAILED)
            return None
        return self.audit.append(
            actor_ref=ctx.actor_ref(),
            tenant_scope=tenant_scope or ctx.tenant_id,
            capability=capability,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            reason=reason,
            request_id=ctx.request_id,
        )

    def dashboard(self, ctx: RequestSecurityContext, *, window: str = "1h") -> OperationsDashboardView:
        self.access.require(ctx, PERM_OPS_READ)
        self.obs.emit("admin.dashboard.view", metadata={"window": window})
        return OperationsDashboardView(
            health=self.system_health(ctx),
            active_runs=self._count_runs(status={"running", "queued", "waiting"}),
            queued_jobs=sum(q.depth for q in self.list_queues(ctx)),
            failed_jobs=self._count_runs(status={"failed", "error"}),
            dlq_count=len(self._dlq_items(ctx)),
            pending_approvals=len(self.list_approvals(ctx)),
            alerts=self.alerts.list_active(),
            cost_summary=self.usage_summary(ctx, window=window),
            generated_at=_utc(),
        )

    def system_health(self, ctx: RequestSecurityContext) -> SystemHealthView:
        self.access.require(ctx, PERM_OPS_READ)
        components = []
        overall = HEALTH_HEALTHY
        try:
            from config.runtime_health import evaluate_readiness

            snap = evaluate_readiness(side_effect_runtime=self.side_effect_runtime)
            for dep in snap.dependencies:
                st = str(dep.status or "unknown").lower()
                mapped = HEALTH_HEALTHY if st == "ok" else HEALTH_DEGRADED if st == "degraded" else HEALTH_UNHEALTHY if st in {"fail", "not_ready"} else HEALTH_UNKNOWN
                components.append({"name": dep.name, "status": mapped, "detail": redact(dep.detail or "")})
                if mapped == HEALTH_UNHEALTHY:
                    overall = HEALTH_UNHEALTHY
                elif mapped == HEALTH_DEGRADED and overall == HEALTH_HEALTHY:
                    overall = HEALTH_DEGRADED
        except Exception as exc:
            components.append({"name": "runtime", "status": HEALTH_UNKNOWN, "detail": redact(str(exc))[:120]})
            overall = HEALTH_UNKNOWN
        self.alerts.evaluate_health(components)
        return SystemHealthView(overall=overall, generated_at=_utc(), components=components, window="live")

    def production_foundation_status(self, ctx: RequestSecurityContext) -> dict:
        self.access.require(ctx, PERM_OPS_READ)
        if self.production_foundation is None:
            return {"status": "UNKNOWN", "detail": "production_foundation_not_wired"}
        status = self.production_foundation.production_status()
        self.production_foundation.evaluate_and_emit_alerts(alert_engine=self.alerts)
        return status

    def list_runs(self, ctx, *, tenant_id=None, status=None, limit=50, offset=0):
        self.access.require(ctx, PERM_OPS_READ)
        tenant = self.access.allowed_tenant_filter(ctx, tenant_id)
        limit = min(max(1, limit), 200)
        offset = max(0, offset)
        if self.workflow_runtime is None:
            return [], 0
        store = getattr(getattr(self.workflow_runtime, "state_manager", None), "_store", None)
        if store is None or not hasattr(store, "list_all"):
            return [], 0
        filtered = [st for st in store.list_all() if (not tenant or getattr(st, "tenant_id", "") == tenant) and (not status or str(getattr(st, "status", "")).lower() == status.lower())]
        page = filtered[offset : offset + limit]
        items = [WorkflowRunView(workflow_id=getattr(st, "workflow_id", ""), tenant_id=getattr(st, "tenant_id", "") or "", status=str(getattr(st, "status", "") or ""), workflow_type=str(getattr(st, "workflow_type", "") or ""), error_code=getattr(st, "error_code", None), queue_task_id=getattr(st, "queue_task_id", None)) for st in page]
        return items, len(filtered)

    def get_run(self, ctx, workflow_id: str, *, tenant_id=None) -> WorkflowRunDetail:
        self.access.require(ctx, PERM_OPS_READ)
        if self.workflow_runtime is None:
            raise AdminError(ADMIN_TARGET_NOT_FOUND)
        tid_filter = tenant_id or (None if self.access.is_platform_scope(ctx) else ctx.tenant_id)
        payload = self.workflow_runtime.get_status(workflow_id, tenant_id=tid_filter)
        if not payload:
            raise AdminError(ADMIN_TARGET_NOT_FOUND)
        tid = str(payload.get("tenant_id") or "")
        self.access.assert_tenant_scope(ctx, tid or ctx.tenant_id)
        base = WorkflowRunView(workflow_id=workflow_id, tenant_id=tid, status=str(payload.get("status") or ""), workflow_type=str(payload.get("workflow_type") or ""), error_code=payload.get("error_code"), queue_task_id=payload.get("queue_task_id"))
        timeline = [{"phase": s.get("status"), "step_id": s.get("step_id")} for s in (payload.get("steps") or [])]
        return WorkflowRunDetail(**base.__dict__, steps=list(payload.get("steps") or []), timeline=timeline, progress=dict(payload.get("progress") or {}))

    async def cancel_run(self, ctx, cmd: CancelRunCommand) -> dict:
        self.access.require(ctx, PERM_OPS_WRITE)
        self.access.assert_tenant_scope(ctx, cmd.tenant_id)
        current = self.workflow_runtime.get_status(cmd.workflow_id, tenant_id=cmd.tenant_id)
        if not current:
            raise AdminError(ADMIN_TARGET_NOT_FOUND)
        if cmd.expected_status and str(current.get("status")) != cmd.expected_status:
            raise AdminError(ADMIN_STALE_STATE)
        result = await self.workflow_runtime.cancel(cmd.workflow_id, tenant_id=cmd.tenant_id)
        self._audit(ctx, capability=PERM_OPS_WRITE, action="run.cancel", target_type="workflow", target_id=cmd.workflow_id, result="ok", reason=cmd.reason, tenant_scope=cmd.tenant_id)
        self.obs.emit("admin.run.cancel", metadata={"workflow_id": cmd.workflow_id})
        return result if isinstance(result, dict) else {"status": "cancelled"}

    def list_queues(self, ctx) -> list[QueueView]:
        self.access.require(ctx, PERM_OPS_READ)
        q = getattr(self.workflow_runtime, "queue", None) if self.workflow_runtime else None
        if q is None:
            return []
        from task_queue.lanes import EXECUTION_LANES

        out = []
        for lane in EXECUTION_LANES:
            depth = active = retry = 0
            if hasattr(q.store, "count_by_lane_status"):
                try:
                    depth = q.store.count_by_lane_status(lane, "queued")
                    active = q.store.count_by_lane_status(lane, "running") + q.store.count_by_lane_status(lane, "leased")
                    retry = q.store.count_by_lane_status(lane, "retry_wait")
                except Exception:
                    pass
            out.append(QueueView(lane=lane, depth=depth, active=active, retry_wait=retry, generated_at=_utc()))
        return out

    def list_workers(self, ctx) -> list[WorkerPoolView]:
        self.access.require(ctx, PERM_OPS_READ)
        from config.runtime_health import DRAIN

        d = DRAIN.is_draining()
        return [WorkerPoolView(pool_name="default", lane="interactive", draining=d, health=HEALTH_DEGRADED if d else HEALTH_HEALTHY)]

    def routing_health(self, ctx) -> RoutingHealthView:
        self.access.require(ctx, PERM_OPS_READ)
        active = self.routing_activation.get_active().policy_version if self.routing_activation and self.routing_activation.get_active() else None
        degraded: list[str] = []
        if self.health_tracker is not None:
            from agents.provider_registry import PROVIDER_IDS

            for pid in PROVIDER_IDS:
                snap = self.health_tracker.snapshot(pid)
                if snap.state in {"degraded", "cooldown"}:
                    degraded.append(pid)
        status = HEALTH_HEALTHY if active and not degraded else HEALTH_DEGRADED if degraded else HEALTH_UNKNOWN if not active else HEALTH_HEALTHY
        return RoutingHealthView(status=status, active_policy_version=active, degraded_providers=degraded, generated_at=_utc())

    def list_providers(self, ctx) -> list[ProviderHealthView]:
        self.access.require(ctx, PERM_OPS_READ)
        if self.health_tracker is None:
            return []
        from agents.provider_registry import PROVIDER_IDS

        out: list[ProviderHealthView] = []
        for pid in PROVIDER_IDS:
            snap = self.health_tracker.snapshot(pid)
            out.append(
                ProviderHealthView(
                    provider=pid,
                    status=_map_provider_health(snap.state),
                    breaker_state=snap.state,
                    throttle_count=snap.recent_failure_count,
                )
            )
        return out

    def list_production_integrations(self, ctx) -> dict:
        self.access.require(ctx, PERM_OPS_READ)
        runtime = self.production_integrations
        if runtime is None:
            return {"providers": [], "credentials": []}
        return {
            "providers": runtime.provider_matrix(),
            "credentials": runtime.bundle.credential_inventory,
            "health": runtime.health(),
        }

    def controlled_launch_handoff(self, ctx) -> dict:
        self.access.require(ctx, PERM_OPS_READ)
        if self.controlled_launch is None:
            return {"available": False}
        return {"available": True, "handoff": self.controlled_launch.get_handoff()}

    def controlled_launch_read(self, ctx, candidate_id: str) -> dict:
        self.access.require(ctx, PERM_OPS_READ)
        if self.controlled_launch is None:
            raise AdminError(ADMIN_TARGET_NOT_FOUND)
        return self.controlled_launch.read_model(ctx, candidate_id)

    def controlled_launch_hold(self, ctx, candidate_id: str, *, reason: str = "") -> dict:
        self.access.require(ctx, PERM_OPS_WRITE)
        if self.controlled_launch is None:
            raise AdminError(ADMIN_TARGET_NOT_FOUND)
        from controlled_launch.commands import HoldRolloutCommand

        return self.controlled_launch.hold(ctx, HoldRolloutCommand(candidate_id=candidate_id, actor_ref=ctx.actor_ref(), reason=reason))

    def controlled_launch_abort(self, ctx, candidate_id: str, *, reason: str = "") -> dict:
        self.access.require(ctx, PERM_OPS_WRITE)
        if self.controlled_launch is None:
            raise AdminError(ADMIN_TARGET_NOT_FOUND)
        from controlled_launch.commands import AbortRolloutCommand

        return self.controlled_launch.abort(ctx, AbortRolloutCommand(candidate_id=candidate_id, actor_ref=ctx.actor_ref(), reason=reason))

    def controlled_launch_rollback(self, ctx, candidate_id: str, *, reason: str = "") -> dict:
        self.access.require(ctx, PERM_OPS_WRITE)
        if self.controlled_launch is None:
            raise AdminError(ADMIN_TARGET_NOT_FOUND)
        from controlled_launch.commands import RollbackRolloutCommand

        return self.controlled_launch.rollback(
            ctx,
            RollbackRolloutCommand(candidate_id=candidate_id, actor_ref=ctx.actor_ref(), reason=reason),
        )

    def production_activation_preflight(self, ctx, candidate_id: str) -> dict:
        self.access.require(ctx, PERM_OPS_READ)
        if self.production_activation is None:
            return {"available": False}
        return {"available": True, **self.production_activation.preflight(ctx, candidate_id)}

    def production_activation_read(self, ctx, candidate_id: str) -> dict:
        self.access.require(ctx, PERM_OPS_READ)
        if self.production_activation is None:
            raise AdminError(ADMIN_TARGET_NOT_FOUND)
        return self.production_activation.read_model(ctx, candidate_id)

    def production_activation_rollback(self, ctx, candidate_id: str, *, reason: str = "") -> dict:
        self.access.require(ctx, PERM_OPS_WRITE)
        if self.production_activation is None:
            raise AdminError(ADMIN_TARGET_NOT_FOUND)
        from production_activation.commands import RollbackProductionCommand

        return self.production_activation.rollback(
            ctx,
            RollbackProductionCommand(candidate_id=candidate_id, operator_ref=ctx.actor_ref(), reason=reason),
        )

    def list_tools(self, ctx) -> list[ToolHealthView]:
        self.access.require(ctx, PERM_OPS_READ)
        reg = getattr(self.side_effect_runtime, "tool_registry", None)
        if reg is None:
            return []
        return [ToolHealthView(tool_id=d.tool_id, version=str(d.version), status="registered", side_effect=d.side_effect) for d in reg.list_tools()]

    def usage_summary(self, ctx, *, window="24h", tenant_id: str | None = None) -> UsageSummaryView:
        self.access.require(ctx, PERM_OPS_COST_READ)
        tenant = self.access.allowed_tenant_filter(ctx, tenant_id)
        if tenant:
            self.access.assert_tenant_scope(ctx, tenant)
        start, end = _window_bounds(window)
        total = Decimal("0")
        tokens = 0
        by_provider: dict[str, Decimal] = {}
        by_model: dict[str, Decimal] = {}
        if self.finops is not None:
            store = self.finops._store
            records = []
            for rec in store.records():
                stamp = rec.timestamp
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if start <= stamp <= end:
                    records.append(rec)
            for rec in records:
                if tenant and rec.tenant_id != tenant:
                    continue
                if rec.estimated_cost is not None:
                    total += rec.estimated_cost
                    by_provider[rec.provider_id] = by_provider.get(rec.provider_id, Decimal("0")) + rec.estimated_cost
                    model_key = f"{rec.provider_id}/{rec.model_id}"
                    by_model[model_key] = by_model.get(model_key, Decimal("0")) + rec.estimated_cost
                if rec.total_tokens:
                    tokens += int(rec.total_tokens)
        return UsageSummaryView(
            window=window,
            total_cost=str(total),
            total_tokens=tokens,
            by_provider={k: str(v) for k, v in by_provider.items()},
            by_model={k: str(v) for k, v in by_model.items()},
            generated_at=_utc(),
        )

    def budget_status(self, ctx, *, tenant_id=None) -> BudgetStatusView:
        self.access.require(ctx, PERM_OPS_COST_READ)
        tid = tenant_id or ctx.tenant_id
        self.access.assert_tenant_scope(ctx, tid)
        if self.budget_guard is None:
            return BudgetStatusView(tenant_id=tid, limit="unknown", used="0", remaining="unknown", status="unknown")
        from finops.budget_models import SCOPE_TENANT

        limit_str = "unknown"
        used_str = "0"
        remaining_str = "unknown"
        status = "unknown"
        for policy in self.budget_guard.policies:
            if policy.scope != SCOPE_TENANT:
                continue
            if policy.scope_key and policy.scope_key != tid:
                continue
            if policy.hard_limit is not None:
                limit_str = str(policy.hard_limit)
                used = self.budget_guard.ledger.get_spent(SCOPE_TENANT, tid)
                used_str = str(used)
                remaining = self.budget_guard.get_remaining_budget(SCOPE_TENANT, tid)
                remaining_str = str(remaining) if remaining is not None else "unknown"
                status = "ok" if remaining is None or remaining >= 0 else "exceeded"
                break
        return BudgetStatusView(tenant_id=tid, limit=limit_str, used=used_str, remaining=remaining_str, status=status)

    def _dlq_items(self, ctx):
        q = getattr(self.workflow_runtime, "queue", None) if self.workflow_runtime else None
        if q is None:
            return []
        tenant = None if self.access.is_platform_scope(ctx) else ctx.tenant_id
        items = []
        for task in q.get_dead_letters():
            tid = str(getattr(task, "tenant_id", "") or "")
            if tenant and tid != tenant:
                continue
            items.append(DLQItemView(task_id=task.queue_task_id, tenant_id=tid, operation=str(getattr(task, "task_type", "") or "task"), status=task.status, error_code=getattr(task, "error_code", None), attempt_count=int(getattr(task, "attempt", 0) or 0), created_at=str(getattr(task, "created_at", "") or ""), redrive_eligible=True, summary=redact(str(getattr(task, "error_code", "") or ""))))
        return items

    def list_dlq(self, ctx, *, limit=50, offset=0):
        self.access.require(ctx, PERM_OPS_READ)
        items = self._dlq_items(ctx)
        return items[offset : offset + min(limit, 200)], len(items)

    def list_failures(self, ctx, *, limit=50, offset=0):
        dlq, total = self.list_dlq(ctx, limit=limit, offset=offset)
        return [FailureView(failure_id=d.task_id, tenant_id=d.tenant_id, operation=d.operation, error_code=d.error_code or "unknown", created_at=d.created_at, summary=d.summary) for d in dlq], total

    def redrive_dlq(self, ctx, cmd: RedriveDLQCommand) -> dict:
        self.access.require(ctx, PERM_OPS_RECOVERY)
        self.access.assert_tenant_scope(ctx, cmd.tenant_id)
        key = f"redrive:{cmd.idempotency_key}"
        if key in self._processed_idempotency:
            return {"status": "already_redriven", "task_id": cmd.task_id}
        q = getattr(self.workflow_runtime, "queue", None)
        task = q.redrive_dead_letter(cmd.task_id, actor_ref=ctx.actor_ref(), tenant_id=cmd.tenant_id)
        self._processed_idempotency.add(key)
        self._audit(ctx, capability=PERM_OPS_RECOVERY, action="dlq.redrive", target_type="queue_task", target_id=cmd.task_id, result="ok", reason=cmd.reason, tenant_scope=cmd.tenant_id)
        self.obs.emit("admin.dlq.redrive", metadata={"task_id": cmd.task_id})
        return {"status": task.status, "task_id": task.queue_task_id}

    def list_approvals(self, ctx, *, status="pending") -> list[ApprovalQueueView]:
        self.access.require(ctx, PERM_OPS_READ)
        if self.approval_store is None:
            return []
        tenant = None if self.access.is_platform_scope(ctx) else ctx.tenant_id
        if status == "pending" and hasattr(self.approval_store, "list_pending"):
            records = self.approval_store.list_pending()
        elif hasattr(self.approval_store, "list_by_status"):
            records = self.approval_store.list_by_status(status)
        else:
            records = ()
        out: list[ApprovalQueueView] = []
        for rec in records:
            tid = str(getattr(rec, "tenant_id", "") or "")
            if tenant and tid and tid != tenant:
                continue
            self.access.assert_tenant_scope(ctx, tid or ctx.tenant_id)
            out.append(
                ApprovalQueueView(
                    approval_id=rec.approval_id,
                    workflow_id=str(getattr(rec, "workflow_id", "") or ""),
                    tenant_id=tid,
                    status=str(getattr(rec, "status", "") or ""),
                    summary=redact(str(getattr(rec, "action_summary", "") or getattr(rec, "action_id", "") or ""))[:200],
                    created_at=str(getattr(rec, "created_at", "") or ""),
                    expires_at=str(getattr(rec, "expires_at", "") or "") or None,
                )
            )
        return out

    def list_side_effects(self, ctx, *, limit=50, offset=0):
        self.access.require(ctx, PERM_OPS_READ)
        limit = min(max(1, limit), 200)
        offset = max(0, offset)
        store = self.execution_store
        if store is None or not hasattr(store, "list_all"):
            return [], 0
        tenant = None if self.access.is_platform_scope(ctx) else ctx.tenant_id
        rows = list(store.list_all())
        if tenant:
            rows = [r for r in rows if str(getattr(r, "tenant_id", "") or "") == tenant]
        page = rows[offset : offset + limit]
        items = [
            SideEffectView(
                execution_id=str(getattr(r, "execution_id", "") or ""),
                tenant_id=str(getattr(r, "tenant_id", "") or ""),
                status=str(getattr(r, "status", "") or ""),
                tool_id=str(getattr(r, "tool_id", "") or ""),
                workflow_id=str(getattr(r, "workflow_id", "") or ""),
                error_code=getattr(r, "error_code", None),
            )
            for r in page
        ]
        return items, len(rows)

    def list_tenants(self, ctx) -> list[TenantUsageView]:
        self.access.require(ctx, PERM_OPS_TENANT_READ)
        tenants = {}
        runs, _ = self.list_runs(ctx, limit=200)
        for r in runs:
            t = tenants.setdefault(r.tenant_id, TenantUsageView(tenant_id=r.tenant_id, active_runs=0, queued_jobs=0, failed_jobs=0))
            if r.status.lower() in {"running", "active"}:
                t.active_runs += 1
            elif r.status.lower() in {"failed", "error"}:
                t.failed_jobs += 1
        if self.saas_store is not None:
            for tid in list(tenants.keys()):
                sub = self.saas_store.get_subscription_for_tenant(tid)
                if sub is not None:
                    tenants[tid].cost_summary = sub.status
        return list(tenants.values())

    def list_audit(self, ctx, *, limit=50, offset=0, **filters):
        self.access.require(ctx, PERM_OPS_SECURITY_READ)
        if self.audit is None:
            return [], 0
        tenant = self.access.allowed_tenant_filter(ctx, filters.get("tenant_scope"))
        return self.audit.list_events(tenant_scope=tenant, action=filters.get("action"), limit=limit, offset=offset)

    def list_alerts(self, ctx):
        self.access.require(ctx, PERM_OPS_READ)
        return self.alerts.list_active()

    def worker_drain(self, ctx, *, reason="") -> dict:
        self.access.require(ctx, PERM_OPS_WRITE)
        from config.runtime_health import DRAIN

        DRAIN.begin_drain()
        self._audit(ctx, capability=PERM_OPS_WRITE, action="worker.drain", target_type="worker_pool", target_id="default", result="ok", reason=reason)
        self.obs.emit("admin.worker.drain", metadata={})
        return {"draining": True}

    def worker_resume(self, ctx, *, reason="") -> dict:
        self.access.require(ctx, PERM_OPS_WRITE)
        from config.runtime_health import DRAIN

        DRAIN.clear_drain()
        self._audit(ctx, capability=PERM_OPS_WRITE, action="worker.resume", target_type="worker_pool", target_id="default", result="ok", reason=reason)
        self.obs.emit("admin.worker.resume", metadata={})
        return {"draining": False}

    def activate_routing(self, ctx, cmd: ActivateRoutingCommand, *, candidate) -> dict:
        self.access.require(ctx, PERM_OPS_ROUTING_WRITE)
        if cmd.confirmation_token != confirmation_token(actor_ref=ctx.actor_ref(), action="routing.activate", target_id=cmd.candidate_id):
            raise AdminError(ADMIN_CONFIRMATION_INVALID)
        if self.routing_activation is None:
            raise AdminError(ADMIN_ACTION_NOT_ALLOWED)
        rec = self.routing_activation.activate(candidate, actor_ref=ctx.actor_ref(), expected_policy_version=cmd.expected_policy_version)
        self._audit(ctx, capability=PERM_OPS_ROUTING_WRITE, action="routing.activate", target_type="routing_policy", target_id=cmd.candidate_id, result="ok", reason=cmd.reason)
        return rec.as_dict()

    def rollback_routing(self, ctx, cmd: RollbackRoutingCommand) -> dict:
        self.access.require(ctx, PERM_OPS_ROUTING_WRITE)
        if cmd.confirmation_token != confirmation_token(actor_ref=ctx.actor_ref(), action="routing.rollback", target_id="active"):
            raise AdminError(ADMIN_CONFIRMATION_INVALID)
        if self.routing_activation is None:
            raise AdminError(ADMIN_ACTION_NOT_ALLOWED)
        previous = self.routing_activation.rollback(ctx.actor_ref())
        self._audit(
            ctx,
            capability=PERM_OPS_ROUTING_WRITE,
            action="routing.rollback",
            target_type="routing_policy",
            target_id=str(getattr(previous, "candidate_id", "") or "none"),
            result="ok",
            reason=cmd.reason,
        )
        return {"restored": previous.as_dict() if previous else None}

    def decide_approval(self, ctx, cmd: ApprovalDecisionCommand) -> dict:
        self.access.require(ctx, PERM_OPS_APPROVAL)
        self.access.assert_tenant_scope(ctx, cmd.tenant_id)
        key = f"approval:{cmd.idempotency_key}"
        if key in self._processed_idempotency:
            return {"status": "already_decided", "approval_id": cmd.approval_id}
        if self.hitl_service is None:
            raise AdminError(ADMIN_ACTION_NOT_ALLOWED)
        from security.hitl_auth import HitlHttpAuthorizer
        from security.hitl_auth import HitlActionPayload

        hitl_http = HitlHttpAuthorizer()
        state = None
        if self.workflow_runtime is not None:
            sm = getattr(self.workflow_runtime, "state_manager", None)
            if sm is not None:
                state = sm.get(cmd.workflow_id)
        payload = HitlActionPayload(tenant_id=cmd.tenant_id)
        if cmd.decision == "approve":
            record = hitl_http.approve(ctx, approval_id=cmd.approval_id, workflow_id=cmd.workflow_id, hitl=self.hitl_service, workflow_state=state, payload=payload)
        elif cmd.decision == "deny":
            record = hitl_http.reject(ctx, approval_id=cmd.approval_id, workflow_id=cmd.workflow_id, hitl=self.hitl_service, workflow_state=state, payload=payload)
        else:
            raise AdminError(ADMIN_ACTION_NOT_ALLOWED)
        self._processed_idempotency.add(key)
        self._audit(
            ctx,
            capability=PERM_OPS_APPROVAL,
            action=f"approval.{cmd.decision}",
            target_type="approval",
            target_id=cmd.approval_id,
            result="ok",
            reason=cmd.reason,
            tenant_scope=cmd.tenant_id,
        )
        return {"approval_id": record.approval_id, "status": record.status}

    def _count_runs(self, *, status):
        if self.workflow_runtime is None:
            return 0
        store = getattr(getattr(self.workflow_runtime, "state_manager", None), "_store", None)
        if store is None or not hasattr(store, "list_all"):
            return 0
        return sum(1 for st in store.list_all() if str(getattr(st, "status", "")).lower() in status)
