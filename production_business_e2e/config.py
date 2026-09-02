"""Production Business E2E configuration flags."""

from __future__ import annotations

import os

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

DEFAULT_PROVIDERS = (
    "ozon",
    "wildberries",
    "yandex_market",
    "bitrix",
    "onec",
    "email",
    "calendar",
    "crm",
)


def production_business_e2e_live_active() -> bool:
    return str(os.environ.get("PRODUCTION_BUSINESS_E2E_MODE", "FIXTURE")).upper() == "LIVE"


def production_business_e2e_live_verified() -> bool:
    return False


def production_business_e2e_engineering_ready() -> bool:
    return True
