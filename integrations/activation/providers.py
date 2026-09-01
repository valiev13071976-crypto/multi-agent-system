"""Canonical provider catalog for Real Integration Activation."""

from __future__ import annotations

from integrations.activation.models import (
    AUTH_API_KEY,
    AUTH_BASIC,
    AUTH_BEARER,
    AUTH_OAUTH2,
    AUTH_SERVICE_ACCOUNT,
    ENV_FIXTURE,
    ENV_LIVE,
    ENV_SANDBOX,
    IntegrationProvider,
)

BITRIX = IntegrationProvider(
    provider_id="bitrix",
    provider_type="cms_crm",
    display_name="1C-Bitrix / Bitrix24",
    capabilities=(
        "cms.bitrix",
        "cms.bitrix.catalog.read",
        "cms.bitrix.catalog.write",
        "bitrix.read",
        "bitrix.write",
    ),
    auth_types=(AUTH_API_KEY, AUTH_OAUTH2),
    read_capabilities=("cms.bitrix", "cms.bitrix.catalog.read", "bitrix.read"),
    write_capabilities=("cms.bitrix.catalog.write", "bitrix.write"),
    adapter_id="bitrix",
    notes="Native commerce CMS boundary; Aspro is profile over Bitrix",
)

ASPRO = IntegrationProvider(
    provider_id="aspro",
    provider_type="cms",
    display_name="Aspro Premier",
    capabilities=("aspro.read", "aspro.write", "cms.bitrix"),
    auth_types=(AUTH_API_KEY, AUTH_OAUTH2),
    read_capabilities=("aspro.read", "cms.bitrix"),
    write_capabilities=("aspro.write",),
    adapter_id="aspro",
    notes="Profile/mapping over Bitrix — not a separate core",
)

ONEC = IntegrationProvider(
    provider_id="onec",
    provider_type="erp",
    display_name="1C",
    capabilities=("erp.1c", "erp.1c.catalog.read", "erp.1c.catalog.write", "onec.read", "onec.write"),
    auth_types=(AUTH_BASIC, AUTH_OAUTH2, AUTH_SERVICE_ACCOUNT, AUTH_API_KEY),
    read_capabilities=("erp.1c", "erp.1c.catalog.read", "onec.read"),
    write_capabilities=("erp.1c.catalog.write", "onec.write"),
    adapter_id="onec",
)

WILDBERRIES = IntegrationProvider(
    provider_id="wildberries",
    provider_type="marketplace",
    display_name="Wildberries",
    capabilities=(
        "marketplace.product",
        "marketplace.wb.stock.read",
        "marketplace.wb.price.read",
        "marketplace.wb.price.write",
        "marketplace.wb.orders.read",
    ),
    auth_types=(AUTH_API_KEY, AUTH_BEARER),
    read_capabilities=("marketplace.product", "marketplace.wb.stock.read", "marketplace.wb.price.read", "marketplace.wb.orders.read"),
    write_capabilities=("marketplace.wb.price.write",),
    adapter_id="wildberries",
)

OZON = IntegrationProvider(
    provider_id="ozon",
    provider_type="marketplace",
    display_name="Ozon",
    capabilities=(
        "marketplace.product",
        "marketplace.ozon.price.read",
        "marketplace.ozon.price.write",
        "marketplace.ozon.orders.read",
        "marketplace.ozon.stock.read",
    ),
    auth_types=(AUTH_API_KEY, AUTH_BEARER),
    read_capabilities=("marketplace.product", "marketplace.ozon.price.read", "marketplace.ozon.orders.read", "marketplace.ozon.stock.read"),
    write_capabilities=("marketplace.ozon.price.write",),
    adapter_id="ozon",
)

YANDEX_MARKET = IntegrationProvider(
    provider_id="yandex_market",
    provider_type="marketplace",
    display_name="Yandex Market",
    capabilities=(
        "marketplace.product",
        "marketplace.yandex.orders.read",
        "marketplace.yandex.price.read",
        "marketplace.yandex.stock.read",
    ),
    auth_types=(AUTH_API_KEY, AUTH_OAUTH2),
    read_capabilities=("marketplace.product", "marketplace.yandex.orders.read", "marketplace.yandex.price.read", "marketplace.yandex.stock.read"),
    write_capabilities=(),  # fixture readiness: read-heavy by default
    adapter_id="yandex_market",
)

EMAIL = IntegrationProvider(
    provider_id="email",
    provider_type="productivity",
    display_name="Email",
    capabilities=("email", "email.send", "email.read"),
    auth_types=(AUTH_OAUTH2, AUTH_API_KEY),
    read_capabilities=("email", "email.read"),
    write_capabilities=("email.send",),
    adapter_id="email",
)

CALENDAR = IntegrationProvider(
    provider_id="calendar",
    provider_type="productivity",
    display_name="Calendar",
    capabilities=("calendar", "calendar.read", "calendar.write"),
    auth_types=(AUTH_OAUTH2,),
    read_capabilities=("calendar", "calendar.read"),
    write_capabilities=("calendar.write",),
    adapter_id="calendar",
)

CRM = IntegrationProvider(
    provider_id="crm",
    provider_type="crm",
    display_name="CRM",
    capabilities=("crm", "crm.read", "crm.write"),
    auth_types=(AUTH_OAUTH2, AUTH_API_KEY),
    read_capabilities=("crm", "crm.read"),
    write_capabilities=("crm.write",),
    adapter_id="crm",
)

ANALYTICS = IntegrationProvider(
    provider_id="analytics",
    provider_type="analytics",
    display_name="Analytics",
    capabilities=("analytics.read",),
    auth_types=(AUTH_SERVICE_ACCOUNT, AUTH_OAUTH2),
    read_capabilities=("analytics.read",),
    write_capabilities=(),
    adapter_id="analytics",
)

COMPOSIO = IntegrationProvider(
    provider_id="composio",
    provider_type="integration_broker",
    display_name="Composio",
    capabilities=(
        "composio",
        "email",
        "email.send",
        "email.read",
        "calendar",
        "calendar.read",
        "drive.file.read",
        "slack.message.send",
    ),
    auth_types=(AUTH_API_KEY, AUTH_OAUTH2),
    read_capabilities=("composio", "email.read", "calendar.read", "drive.file.read"),
    write_capabilities=("email.send", "slack.message.send", "calendar.write"),
    adapter_id="composio",
    supports_triggers=True,
    notes="OPTIONAL adapter/provider — NOT the Panda Tool Platform",
    supported_environments=(ENV_FIXTURE, ENV_SANDBOX, ENV_LIVE),
)

PROVIDER_CATALOG: dict[str, IntegrationProvider] = {
    p.provider_id: p
    for p in (
        BITRIX,
        ASPRO,
        ONEC,
        WILDBERRIES,
        OZON,
        YANDEX_MARKET,
        EMAIL,
        CALENDAR,
        CRM,
        ANALYTICS,
        COMPOSIO,
    )
}


def list_providers() -> tuple[IntegrationProvider, ...]:
    return tuple(PROVIDER_CATALOG.values())


def get_provider(provider_id: str) -> IntegrationProvider | None:
    return PROVIDER_CATALOG.get(provider_id)
