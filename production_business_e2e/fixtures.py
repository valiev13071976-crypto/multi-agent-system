"""Deterministic E2E fixture world — external boundary only."""

from __future__ import annotations

from decimal import Decimal

from business_assistant.service import BusinessAssistantService
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from production_business_e2e.config import DEFAULT_PROVIDERS, TENANT_A, TENANT_B


def activate_provider(
    svc: IntegrationActivationService,
    *,
    tenant: str,
    provider: str,
    env: str = ENV_FIXTURE,
) -> str:
    ref = svc.put_secret_ref(
        tenant_id=tenant,
        secret_ref=f"secret:{provider}-{tenant}",
        value=f"tok-{provider}-{tenant}",
    )
    conn = svc.configure_connection(
        tenant_id=tenant,
        provider_id=provider,
        credential_ref=ref,
        environment=env,
    )
    svc.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)
    return conn.connection_id


def activate_tenant(
    svc: IntegrationActivationService,
    *,
    tenant: str,
    providers: tuple[str, ...] = DEFAULT_PROVIDERS,
) -> dict[str, str]:
    return {p: activate_provider(svc, tenant=tenant, provider=p) for p in providers}


def seed_samsung_supplier(ba: BusinessAssistantService) -> None:
    rows = [
        {"sku": "SAM-1", "brand": "Samsung", "title": "Galaxy A", "price": "2000", "ambiguous": False},
        {"sku": "SAM-2", "brand": "Samsung", "title": "Galaxy B", "price": "500", "ambiguous": False},
        {"sku": "SAM-AMB", "brand": "Samsung", "title": "Galaxy Ambiguous", "price": "1800", "ambiguous": True},
        {"sku": "APL-1", "brand": "Apple", "title": "iPhone", "price": "3000", "ambiguous": False},
    ]
    ba.seed_supplier_fixture(
        rows=rows,
        previous_prices={"SAM-1": "1900", "SAM-2": "800", "APL-1": "2900"},
        market_obs={"SAM-1": "2100", "SAM-2": "600"},
        costs={"SAM-1": "1000", "SAM-2": "1000", "APL-1": "1500"},
        catalog=[{"product_id": f"p-{r['sku']}", "sku_id": r["sku"], "brand": r["brand"]} for r in rows],
    )


def auth_env() -> dict[str, str]:
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-a|tenant-a|user-a|user|secret-a;"
            "key-b|tenant-b|user-b|user|secret-b;"
            "key-view|tenant-a|viewer|viewer|secret-view"
        ),
        "SCHEDULED_AUTOMATION_MODE": "FIXTURE",
        "ANALYTICS_DASHBOARD_MODE": "FIXTURE",
        "PRODUCTION_BUSINESS_E2E_MODE": "FIXTURE",
    }


def api_headers(secret: str = "secret-a") -> dict[str, str]:
    return {"X-API-Key": secret}
