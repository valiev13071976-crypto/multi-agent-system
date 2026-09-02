"""Composed production-shaped runtime for E2E scenarios."""

from __future__ import annotations

from business_assistant.service import BusinessAssistantService
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from marketplace.service import MarketplacePlatformService
from production_business_e2e.config import DEFAULT_PROVIDERS, TENANT_A, TENANT_B
from production_business_e2e.fixtures import activate_tenant
from production_business_e2e.models import E2EWorld


def build_e2e_world(
    *,
    tenants: tuple[str, str] = (TENANT_A, TENANT_B),
    providers: tuple[str, ...] = DEFAULT_PROVIDERS,
    with_analytics: bool = True,
    with_scheduling: bool = True,
) -> E2EWorld:
    from analytics_dashboard.runtime import build_analytics_dashboard_runtime
    from integrations.bitrix.catalog import BitrixCatalogStore
    from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter
    from integrations.onec.catalog import OneCCatalogStore
    from integrations.onec.fixture_adapter import OneCFixtureAdapter
    from integrations.ozon.catalog import OzonCatalogStore
    from integrations.ozon.fixture_adapter import OzonFixtureAdapter
    from scheduled_automation.runtime import build_scheduled_automation_runtime

    activation = IntegrationActivationService()
    # Isolated fixture stores — E2E writes must not leak into global catalog used by other tests.
    activation._ozon_fixture = OzonFixtureAdapter(store=OzonCatalogStore())
    activation._adapters["ozon"] = activation._ozon_fixture
    activation._bitrix_fixture = BitrixFixtureAdapter(store=BitrixCatalogStore())
    activation._adapters["bitrix"] = activation._bitrix_fixture
    activation._onec_fixture = OneCFixtureAdapter(store=OneCCatalogStore())
    activation._adapters["onec"] = activation._onec_fixture

    for tenant in tenants:
        activate_tenant(activation, tenant=tenant, providers=providers)

    analytics = build_analytics_dashboard_runtime().service if with_analytics else None
    scheduling = build_scheduled_automation_runtime().service if with_scheduling else None

    ba = BusinessAssistantService(
        marketplace=MarketplacePlatformService(),
        integration_activation=activation,
        integration_environment=ENV_FIXTURE,
        analytics_dashboard=analytics,
        scheduled_automation=scheduling,
    )
    return E2EWorld(
        activation=activation,
        ba=ba,
        analytics=analytics,
        scheduling=scheduling,
        tenants=tenants,
    )
