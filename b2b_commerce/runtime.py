"""B2B runtime composition."""

from __future__ import annotations

import os

from b2b_commerce.service import B2BCommerceService
from b2b_commerce.sqlite_store import SqliteB2BStore


def b2b_commerce_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("B2B_COMMERCE_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


class B2BCommerceRuntime:
    def __init__(self, *, service: B2BCommerceService, enabled: bool = True):
        self.service = service
        self.enabled = bool(enabled)

    def health(self) -> dict:
        return {"b2b_status": "healthy" if self.enabled else "disabled", "enabled": self.enabled}

    def close(self) -> None:
        pass


def build_b2b_commerce_runtime(
    *,
    env: dict | None = None,
    store=None,
    db_path: str | None = None,
    telegram_provider=None,
    production_bundle=None,
    product_platform_service=None,
    acquisition_service=None,
    document_service=None,
    observability=None,
) -> B2BCommerceRuntime | None:
    if not b2b_commerce_enabled(env):
        return None
    source = env if env is not None else os.environ
    path = db_path or str(source.get("B2B_COMMERCE_DB_PATH") or ":memory:")
    b2b_store = store or SqliteB2BStore(path)
    tg = telegram_provider
    if tg is None and production_bundle is not None:
        tg = production_bundle.telegram_provider
    if tg is None:
        from integrations.production.adapters.telegram import build_telegram_provider

        tg = build_telegram_provider(source)
    service = B2BCommerceService(
        b2b_store,
        telegram_provider=tg,
        product_platform_service=product_platform_service,
        acquisition_service=acquisition_service,
        document_service=document_service,
        observability=observability,
    )
    return B2BCommerceRuntime(service=service, enabled=True)
