"""Commerce runtime composition."""

from __future__ import annotations

import os

from commerce.gateways import (
    FakeAccountingGateway,
    FakeEdoGateway,
    FakeFiscalGateway,
    FakeFrontOfficeGateway,
    FakeInventoryGateway,
    FakeMarkingGateway,
)
from commerce.rules import ComplianceRulesEngine
from commerce.schedule import CommerceReconciliationScheduler, DEFAULT_INTERVAL_SECONDS
from commerce.service import CommerceService
from commerce.store import CommerceStore
from commerce.workflow_def import register_commerce_workflows


def commerce_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    interval_raw = str(source.get("COMMERCE_RECONCILIATION_INTERVAL_SECONDS") or "").strip()
    try:
        interval = float(interval_raw) if interval_raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        interval = DEFAULT_INTERVAL_SECONDS
    if interval <= 0:
        interval = DEFAULT_INTERVAL_SECONDS
    tenants_raw = str(source.get("COMMERCE_RECONCILIATION_TENANTS") or "").strip()
    tenants = tuple(t.strip() for t in tenants_raw.split(",") if t.strip())
    return {
        "enabled": str(source.get("COMMERCE_ENABLED", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        "db_path": str(source.get("COMMERCE_DB_PATH") or "").strip() or None,
        "use_shared_db": str(source.get("COMMERCE_USE_SHARED_DB", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        # Safe default: scheduling off unless explicitly enabled
        "reconciliation_enabled": str(
            source.get("COMMERCE_RECONCILIATION_ENABLED", "false")
        )
        .strip()
        .lower()
        in {"1", "true", "yes", "on"},
        "reconciliation_interval_seconds": interval,
        "reconciliation_tenants": tenants,
    }


class CommerceRuntime:
    def __init__(
        self,
        *,
        service: CommerceService,
        store: CommerceStore,
        reconciliation_scheduler: CommerceReconciliationScheduler | None = None,
        enabled: bool = True,
        reconciliation_enabled: bool = False,
        reconciliation_interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ):
        self.service = service
        self.store = store
        self.reconciliation_scheduler = reconciliation_scheduler
        self.enabled = enabled
        self.reconciliation_enabled = reconciliation_enabled
        self.reconciliation_interval_seconds = float(reconciliation_interval_seconds)

    def health(self) -> dict:
        schedules = 0
        if self.reconciliation_scheduler is not None:
            schedules = len(
                [
                    s
                    for s in self.reconciliation_scheduler.scheduler.store.list_all()
                    if s.workflow_type == "commerce.reconcile"
                ]
            )
        return {
            "commerce_status": "healthy" if self.enabled else "disabled",
            "persistence_backend": getattr(self.store, "persistence_backend", "sqlite"),
            "connection_mode": getattr(self.store, "connection_mode", "memory"),
            "enabled": self.enabled,
            "reconciliation_enabled": self.reconciliation_enabled,
            "reconciliation_interval_seconds": self.reconciliation_interval_seconds,
            "reconciliation_schedules": schedules,
            "workflows": [
                "commerce.procurement_receive",
                "commerce.b2c_fulfillment",
                "commerce.b2b_own_use",
                "commerce.b2b_resale",
                "commerce.return",
                "commerce.cancel",
                "commerce.reconcile",
            ],
        }

    def ensure_reconciliation_schedules(
        self,
        tenants: list[str] | tuple[str, ...],
        *,
        interval_seconds: float | None = None,
    ) -> list:
        """Register per-tenant commerce.reconcile schedules on the shared WorkflowScheduler."""
        if not self.reconciliation_enabled or self.reconciliation_scheduler is None:
            return []
        interval = (
            float(interval_seconds)
            if interval_seconds is not None
            else self.reconciliation_interval_seconds
        )
        return self.reconciliation_scheduler.register_tenants(
            tenants, interval_seconds=interval
        )

    def close(self) -> None:
        if hasattr(self.store, "close"):
            try:
                self.store.close()
            except Exception:
                pass


def build_commerce_runtime(
    *,
    env: dict | None = None,
    workflow_runtime=None,
    shared_connection=None,
    document_service=None,
    data_intelligence=None,
    acquisition_service=None,
    hitl_service=None,
    observability=None,
) -> CommerceRuntime | None:
    cfg = commerce_config(env)
    if not cfg["enabled"]:
        return None
    if shared_connection is not None and cfg["use_shared_db"]:
        store = CommerceStore(shared_connection=shared_connection)
    elif cfg["db_path"]:
        store = CommerceStore(path=cfg["db_path"])
    else:
        store = CommerceStore(path=":memory:")

    service = CommerceService(
        store=store,
        rules=ComplianceRulesEngine(),
        inventory=FakeInventoryGateway(),
        accounting=FakeAccountingGateway(),
        edo=FakeEdoGateway(),
        marking=FakeMarkingGateway(),
        fiscal=FakeFiscalGateway(),
        front_office=FakeFrontOfficeGateway(),
        workflow_runtime=workflow_runtime,
        hitl_service=hitl_service,
        document_service=document_service,
        data_intelligence=data_intelligence,
        acquisition_service=acquisition_service,
    )
    recon_scheduler = None
    if workflow_runtime is not None:
        try:
            register_commerce_workflows(
                workflow_runtime.definitions, workflow_runtime.platform
            )
        except Exception:
            pass
        engine = getattr(workflow_runtime, "platform", None)
        engine = getattr(engine, "workflow_engine", None) or getattr(
            workflow_runtime, "workflow_engine", None
        )
        if engine is not None:
            engine.commerce_service = service
        # Same WorkflowScheduler instance as workflow runtime
        recon_scheduler = CommerceReconciliationScheduler(workflow_runtime.scheduler)
        if cfg["reconciliation_enabled"]:
            tenants = list(cfg["reconciliation_tenants"])
            if not tenants:
                tenants = store.list_tenant_ids()
            if not tenants:
                # Allow later registration; seed placeholder only when explicitly configured
                tenants = []
            recon_scheduler.register_tenants(
                tenants,
                interval_seconds=cfg["reconciliation_interval_seconds"],
            )
    _ = observability
    return CommerceRuntime(
        service=service,
        store=store,
        reconciliation_scheduler=recon_scheduler,
        enabled=True,
        reconciliation_enabled=bool(cfg["reconciliation_enabled"]),
        reconciliation_interval_seconds=cfg["reconciliation_interval_seconds"],
    )
