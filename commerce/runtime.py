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
from commerce.service import CommerceService
from commerce.store import CommerceStore
from commerce.workflow_def import register_commerce_workflows


def commerce_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    return {
        "enabled": str(source.get("COMMERCE_ENABLED", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        "db_path": str(source.get("COMMERCE_DB_PATH") or "").strip() or None,
        "use_shared_db": str(source.get("COMMERCE_USE_SHARED_DB", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
    }


class CommerceRuntime:
    def __init__(self, *, service: CommerceService, store: CommerceStore, enabled: bool = True):
        self.service = service
        self.store = store
        self.enabled = enabled

    def health(self) -> dict:
        return {
            "commerce_status": "healthy" if self.enabled else "disabled",
            "persistence_backend": getattr(self.store, "persistence_backend", "sqlite"),
            "connection_mode": getattr(self.store, "connection_mode", "memory"),
            "enabled": self.enabled,
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
    _ = observability
    return CommerceRuntime(service=service, store=store, enabled=True)
