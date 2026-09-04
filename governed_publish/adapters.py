"""Offline FIXTURE adapters only. Never import live Bitrix/WB/Ozon/Yandex adapters."""

from __future__ import annotations

import uuid

from governed_publish.contracts import COMP_ROLLBACK_SUPPORTED, COMP_UNSUPPORTED, MODE_FIXTURE


class FixtureSiteAdapter:
    def __init__(self) -> None:
        self._published: dict[str, dict] = {}

    def execute(self, *, tenant_id: str, payload: dict) -> dict:
        rid = f"fixture:site:{uuid.uuid4().hex[:12]}"
        self._published.setdefault(tenant_id, {})[payload.get("XML_ID") or payload.get("external_id") or rid] = dict(payload)
        return {
            "fixture_reference": rid,
            "mode": MODE_FIXTURE,
            "live": False,
            "compensation": COMP_ROLLBACK_SUPPORTED,
        }


class FixtureMarketplaceAdapter:
    def execute(self, *, tenant_id: str, target: str, payload: dict) -> dict:
        rid = f"fixture:{target.lower()}:{uuid.uuid4().hex[:12]}"
        return {
            "fixture_reference": rid,
            "mode": MODE_FIXTURE,
            "live": False,
            "target": target,
            "compensation": COMP_UNSUPPORTED,
            "seller_sku": payload.get("seller_sku"),
        }
