"""Runtime wiring for Business Assistant API."""

from __future__ import annotations

import os
from dataclasses import dataclass

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

    def close(self) -> None:
        self.service.close()


def build_business_assistant_api_runtime(
    *,
    env: dict | None = None,
    db_path: str | None = None,
    with_integration: bool = True,
) -> BusinessAssistantApiRuntime:
    env = dict(env or os.environ)
    path = db_path or env.get("BA_API_DB_PATH") or os.path.join(
        os.environ.get("PANDA_DATA_DIR", "."), "ba_api.sqlite"
    )
    store = SqliteBusinessAssistantApiStore(path)
    activation = IntegrationActivationService() if with_integration else None
    ba = BusinessAssistantService(
        marketplace=MarketplacePlatformService(),
        integration_activation=activation,
        integration_environment=ENV_FIXTURE,
    )
    svc = BusinessAssistantApiService(store=store, ba_service=ba)
    return BusinessAssistantApiRuntime(service=svc, store=store)
