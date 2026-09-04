"""Fixture-only price/stock adapters. Never live marketplace."""

from __future__ import annotations

import uuid

from governed_publish.contracts import MODE_FIXTURE


class FixturePriceAdapter:
    def execute(self, *, tenant_id: str, target: str, payload: dict) -> dict:
        return {
            "fixture_reference": f"fixture:price:{target.lower()}:{uuid.uuid4().hex[:12]}",
            "mode": MODE_FIXTURE,
            "live": False,
            "accepted_price": payload.get("proposed_price"),
        }


class FixtureStockAdapter:
    def execute(self, *, tenant_id: str, target: str, payload: dict) -> dict:
        return {
            "fixture_reference": f"fixture:stock:{target.lower()}:{uuid.uuid4().hex[:12]}",
            "mode": MODE_FIXTURE,
            "live": False,
            "published_stock": payload.get("published"),
        }
