"""Runtime composition for Market Intelligence."""

from __future__ import annotations

import os
from dataclasses import dataclass

from market_intel.config import (
    market_intel_db_path,
    market_intel_default_tenant,
    market_intel_enabled,
    require_durable_market_intel_db,
    telegram_user_live_selected,
)
from market_intel.read_client import FixtureTelegramReadClient, select_telegram_read_client
from market_intel.service import MarketIntelligenceService
from market_intel.session_vault import TelegramSessionVault
from market_intel.store import SqliteMarketIntelStore


@dataclass
class MarketIntelligenceRuntime:
    service: MarketIntelligenceService
    store: SqliteMarketIntelStore
    session_vault: TelegramSessionVault
    live_reading: bool

    def close(self) -> None:
        self.service.close()


def _live_client_factory(env: dict, vault: TelegramSessionVault):
    """Resolve the real MTProto client per (tenant, owner).

    The session is looked up and decrypted at call time and handed
    straight to the client, so a decrypted session never lives on the
    service or the runtime.
    """

    def factory(tenant_id: str, owner_id: str):
        return select_telegram_read_client(
            env, session_string=vault.load_session(tenant_id=tenant_id, owner_id=owner_id)
        )

    return factory


def build_market_intelligence_runtime(
    *,
    env: dict | None = None,
    catalog=None,
    db_path: str | None = None,
    read_client=None,
) -> MarketIntelligenceRuntime:
    env = dict(env or os.environ)
    if not market_intel_enabled(env):
        raise RuntimeError("MARKET_INTEL_ENABLED is false")
    path = db_path or market_intel_db_path(env)
    require_durable_market_intel_db(env, path)
    store = SqliteMarketIntelStore(path)
    vault = TelegramSessionVault(store)
    live = telegram_user_live_selected(env)
    factory = _live_client_factory(env, vault) if (live and read_client is None) else None
    service = MarketIntelligenceService(
        store=store,
        read_client=read_client or (None if factory else FixtureTelegramReadClient()),
        read_client_factory=factory,
        catalog=catalog,
        session_vault=vault,
        default_tenant_id=market_intel_default_tenant(env),
        live_reading=live,
    )
    return MarketIntelligenceRuntime(
        service=service, store=store, session_vault=vault, live_reading=live
    )
