"""Payments runtime composition."""

from __future__ import annotations

import os

from payments.gateways import FakeBankGateway, FakeBitrixPaymentBridge, FakePaymentGateway
from payments.matcher import PaymentMatcher
from payments.policy import PaymentPolicyEngine
from payments.reconcile import PaymentsReconciliationEngine
from payments.schedule import PaymentsReconciliationScheduler, DEFAULT_INTERVAL_SECONDS
from payments.service import PaymentsService
from payments.store import PaymentsStore
from payments.workflow_def import register_payments_workflows


def payments_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    interval_raw = str(source.get("PAYMENTS_RECONCILIATION_INTERVAL_SECONDS") or "").strip()
    try:
        interval = float(interval_raw) if interval_raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        interval = DEFAULT_INTERVAL_SECONDS
    if interval <= 0:
        interval = DEFAULT_INTERVAL_SECONDS
    tenants_raw = str(source.get("PAYMENTS_RECONCILIATION_TENANTS") or "").strip()
    tenants = tuple(t.strip() for t in tenants_raw.split(",") if t.strip())
    return {
        "enabled": str(source.get("PAYMENTS_ENABLED", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        "db_path": str(source.get("PAYMENTS_DB_PATH") or "").strip() or None,
        "use_shared_db": str(source.get("PAYMENTS_USE_SHARED_DB", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        "reconciliation_enabled": str(
            source.get("PAYMENTS_RECONCILIATION_ENABLED", "false")
        )
        .strip()
        .lower()
        in {"1", "true", "yes", "on"},
        "reconciliation_interval_seconds": interval,
        "reconciliation_tenants": tenants,
    }


class PaymentsRuntime:
    def __init__(
        self,
        *,
        service: PaymentsService,
        store: PaymentsStore,
        reconciliation_scheduler: PaymentsReconciliationScheduler | None = None,
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
                    if s.workflow_type == "payments.reconcile"
                ]
            )
        return {
            "payments_status": "healthy" if self.enabled else "disabled",
            "persistence_backend": getattr(self.store, "persistence_backend", "sqlite"),
            "connection_mode": getattr(self.store, "connection_mode", "memory"),
            "enabled": self.enabled,
            "reconciliation_enabled": self.reconciliation_enabled,
            "reconciliation_interval_seconds": self.reconciliation_interval_seconds,
            "reconciliation_schedules": schedules,
            "card_processing": False,
            "workflows": [
                "payments.process_event",
                "payments.ingest_statement",
                "payments.match",
                "payments.allocate",
                "payments.reconcile",
                "payments.prepare_refund",
                "payments.execute_refund",
            ],
            "metrics": dict(getattr(self.service, "metrics", {}) or {}),
        }

    def close(self) -> None:
        if hasattr(self.store, "close"):
            try:
                self.store.close()
            except Exception:
                pass


def build_payments_runtime(
    *,
    env: dict | None = None,
    workflow_runtime=None,
    shared_connection=None,
    commerce_service=None,
    hitl_service=None,
    integration_runtime=None,
    data_intelligence=None,
    observability=None,
) -> PaymentsRuntime | None:
    cfg = payments_config(env)
    if not cfg["enabled"]:
        return None
    if shared_connection is not None and cfg["use_shared_db"]:
        store = PaymentsStore(shared_connection=shared_connection)
    elif cfg["db_path"]:
        store = PaymentsStore(path=cfg["db_path"])
    else:
        store = PaymentsStore(path=":memory:")

    policy = PaymentPolicyEngine()
    gateway = FakePaymentGateway()
    bank = FakeBankGateway()
    webhooks = None
    if integration_runtime is not None:
        webhooks = getattr(integration_runtime, "webhooks", None) or getattr(
            getattr(integration_runtime, "service", None), "webhooks", None
        )

    service = PaymentsService(
        store=store,
        payment_gateway=gateway,
        bank_gateway=bank,
        policy_engine=policy,
        matcher=PaymentMatcher(policy),
        recon_engine=PaymentsReconciliationEngine(policy),
        workflow_runtime=workflow_runtime,
        hitl=hitl_service,
        commerce_service=commerce_service,
        integration_webhooks=webhooks,
        data_intelligence=data_intelligence,
        observability=observability,
        bitrix_bridge=FakeBitrixPaymentBridge(),
    )

    recon_scheduler = None
    if workflow_runtime is not None:
        try:
            register_payments_workflows(
                workflow_runtime.definitions, workflow_runtime.platform
            )
        except Exception:
            pass
        engine = getattr(workflow_runtime, "platform", None)
        engine = getattr(engine, "workflow_engine", None) or getattr(
            workflow_runtime, "workflow_engine", None
        )
        if engine is not None:
            engine.payments_service = service
        recon_scheduler = PaymentsReconciliationScheduler(workflow_runtime.scheduler)
        if cfg["reconciliation_enabled"]:
            tenants = list(cfg["reconciliation_tenants"]) or store.list_tenant_ids()
            recon_scheduler.register_tenants(
                tenants,
                interval_seconds=cfg["reconciliation_interval_seconds"],
            )

    return PaymentsRuntime(
        service=service,
        store=store,
        reconciliation_scheduler=recon_scheduler,
        enabled=True,
        reconciliation_enabled=bool(cfg["reconciliation_enabled"]),
        reconciliation_interval_seconds=cfg["reconciliation_interval_seconds"],
    )
