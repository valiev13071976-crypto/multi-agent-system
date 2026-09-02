"""Runtime wiring for Business Assistant API."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
from business_assistant.service import BusinessAssistantService
from business_assistant_api.service import BusinessAssistantApiService
from business_assistant_api.store import SqliteBusinessAssistantApiStore
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from marketplace.service import MarketplacePlatformService


@dataclass
class BusinessAssistantApiRuntime:
    service: BusinessAssistantApiService
    store: SqliteBusinessAssistantApiStore
    upload_dir: str

    def close(self) -> None:
        self.service.close()


def build_business_assistant_api_runtime(
    *,
    env: dict | None = None,
    db_path: str | None = None,
    with_integration: bool = True,
    conversation_gateway=None,
) -> BusinessAssistantApiRuntime:
    env = dict(env or os.environ)
    path = db_path or env.get("BA_API_DB_PATH") or os.path.join(
        os.environ.get("PANDA_DATA_DIR", "."), "ba_api.sqlite"
    )
    upload_dir = env.get("BA_API_UPLOAD_DIR") or os.path.join(
        os.environ.get("PANDA_DATA_DIR", "."), "ba_uploads"
    )
    os.makedirs(upload_dir, exist_ok=True)
    store = SqliteBusinessAssistantApiStore(path)
    activation = IntegrationActivationService() if with_integration else None
    ba = BusinessAssistantService(
        marketplace=MarketplacePlatformService(),
        integration_activation=activation,
        integration_environment=ENV_FIXTURE,
        conversation_gateway=conversation_gateway,
    )
    svc = BusinessAssistantApiService(store=store, ba_service=ba)
    svc.upload_dir = upload_dir
    return BusinessAssistantApiRuntime(service=svc, store=store, upload_dir=upload_dir)


def wire_panda_conversation_gateway(
    *,
    ba_service: BusinessAssistantService,
    workflow_engine,
    run_router,
    context_manager,
    logger: logging.Logger | None = None,
) -> bool:
    """Attach Panda conversational gateway; return False when engine unavailable."""
    log = logger or logging.getLogger(__name__)
    if workflow_engine is None:
        log.warning("Panda conversation gateway not wired: workflow_engine unavailable")
        return False
    if run_router is None or context_manager is None:
        log.error(
            "Panda conversation gateway wiring incomplete: run_router=%s context_manager=%s",
            run_router is not None,
            context_manager is not None,
        )
        raise RuntimeError("panda_conversation_gateway_wiring_incomplete")
    ba_service.conversation_gateway = WorkflowPandaConversationGateway(
        workflow_engine=workflow_engine,
        run_router=run_router,
        context_manager=context_manager,
    )
    return True
