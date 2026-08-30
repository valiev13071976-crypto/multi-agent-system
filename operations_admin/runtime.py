"""Build operations admin runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass

from operations_admin.access import AdminAuthorizationPolicy
from operations_admin.alerts import AlertEngine
from operations_admin.audit_store import AdminAuditStore
from operations_admin.observability import AdminObservability
from operations_admin.service import OperationsAdminService


@dataclass
class OperationsAdminRuntime:
    service: OperationsAdminService
    audit_store: AdminAuditStore
    policy: AdminAuthorizationPolicy

    def close(self) -> None:
        self.audit_store.close()


def build_operations_admin_runtime(
    *,
    side_effect_runtime=None,
    router=None,
    saas_store=None,
    env: dict | None = None,
) -> OperationsAdminRuntime:
    source = env if env is not None else os.environ
    db_path = source.get("OPS_ADMIN_DB_PATH") or source.get("SIDE_EFFECT_DB_PATH") or "data/ops_admin.sqlite"
    if db_path.endswith(".sqlite"):
        db_path = db_path.replace(".sqlite", "_ops_admin.sqlite")
    audit_store = AdminAuditStore(db_path)
    policy = AdminAuthorizationPolicy()
    workflow_runtime = getattr(side_effect_runtime, "workflow_runtime", None) if side_effect_runtime else None
    persistence = getattr(side_effect_runtime, "persistence", None) if side_effect_runtime else None
    approval_store = getattr(persistence, "approval_store", None) if persistence else None
    execution_store = getattr(persistence, "execution_store", None) if persistence else None
    hitl_service = getattr(side_effect_runtime, "hitl_service", None) if side_effect_runtime else None
    finops = getattr(router, "finops", None) if router else None
    budget_guard = getattr(router, "budget_guard", None) if router else None
    health_tracker = getattr(router, "health_tracker", None) if router else None
    routing_activation = getattr(router, "routing_activation", None) if router else None
    service = OperationsAdminService(
        access=policy,
        audit_store=audit_store,
        alert_engine=AlertEngine(),
        side_effect_runtime=side_effect_runtime,
        workflow_runtime=workflow_runtime,
        routing_activation=routing_activation,
        finops=finops,
        budget_guard=budget_guard,
        approval_store=approval_store,
        execution_store=execution_store,
        hitl_service=hitl_service,
        health_tracker=health_tracker,
        saas_store=saas_store,
        obs=AdminObservability(),
        production_foundation=None,
        require_audit=True,
    )
    return OperationsAdminRuntime(service=service, audit_store=audit_store, policy=policy)
