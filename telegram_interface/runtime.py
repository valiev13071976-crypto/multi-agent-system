"""Runtime wiring for Telegram interface."""

from __future__ import annotations

import os
from dataclasses import dataclass

from b2b_commerce.providers.fake_telegram import FakeTelegramProvider
from business_assistant_api.runtime import build_business_assistant_api_runtime
from business_assistant_api.service import BusinessAssistantApiService
from integrations.production.adapters.telegram import ProductionTelegramProvider, build_telegram_provider
from security.rate_limit import RateLimiter
from telegram_interface.config import (
    require_durable_telegram_db,
    require_webhook_secret_in_production,
    telegram_interface_db_path,
    telegram_interface_enabled,
    telegram_live_active,
    telegram_live_network_selected,
    telegram_token_configured,
    telegram_webhook_secret,
)
from telegram_interface.service import TelegramInterfaceService
from telegram_interface.store import SqliteTelegramInterfaceStore
from telegram_interface.transport import ProviderTelegramTransport


@dataclass
class TelegramInterfaceRuntime:
    service: TelegramInterfaceService
    store: SqliteTelegramInterfaceStore
    ba_api: BusinessAssistantApiService
    webhook_secret: str

    def close(self) -> None:
        self.service.close()


def select_telegram_interface_provider(env: dict):
    """Fake unless LIVE is explicitly selected; then ProductionTelegramProvider or fail closed."""
    if not telegram_live_network_selected(env):
        return FakeTelegramProvider()
    if not telegram_token_configured(env):
        raise RuntimeError("TELEGRAM_BOT_TOKEN required when TELEGRAM_LIVE_ACTIVE and TELEGRAM_ENABLED")
    provider = build_telegram_provider(env)
    if not isinstance(provider, ProductionTelegramProvider):
        raise RuntimeError("explicit LIVE Telegram selection cannot fall back to FakeTelegramProvider")
    return provider


def build_telegram_interface_runtime(
    *,
    env: dict | None = None,
    ba_api: BusinessAssistantApiService | None = None,
    db_path: str | None = None,
    upload_dir: str | None = None,
) -> TelegramInterfaceRuntime:
    env = dict(env or os.environ)
    if not telegram_interface_enabled(env):
        raise RuntimeError("TELEGRAM_INTERFACE_ENABLED is false")
    require_webhook_secret_in_production(env)
    path = db_path or telegram_interface_db_path(env)
    require_durable_telegram_db(env, path)
    store = SqliteTelegramInterfaceStore(path)
    if ba_api is None:
        ba_rt = build_business_assistant_api_runtime(env=env, db_path=env.get("BA_API_DB_PATH"))
        ba_api = ba_rt.service
        upload = upload_dir or ba_rt.upload_dir
    else:
        upload = upload_dir or getattr(ba_api, "upload_dir", "")
    live_network = telegram_live_network_selected(env)
    provider = select_telegram_interface_provider(env)
    tenant = str(env.get("TELEGRAM_DEFAULT_TENANT") or "tenant-a")
    transport = ProviderTelegramTransport(provider=provider, tenant_id=tenant, live_network=live_network)
    svc = TelegramInterfaceService(
        store=store,
        ba_api=ba_api,
        transport=transport,
        upload_dir=upload,
        default_tenant_id=tenant,
        live_active=telegram_live_active(env),
        rate_limiter=RateLimiter(),
    )
    svc.live_network = live_network
    ba_core = getattr(ba_api, "ba", None)
    if (
        not live_network
        and ba_core is not None
        and getattr(ba_core, "conversation_gateway", None) is None
    ):
        from business_assistant.conversation_gateway import FakePandaConversationGateway

        ba_core.conversation_gateway = FakePandaConversationGateway(response="Panda fixture reply")
    return TelegramInterfaceRuntime(
        service=svc,
        store=store,
        ba_api=ba_api,
        webhook_secret=telegram_webhook_secret(env),
    )
