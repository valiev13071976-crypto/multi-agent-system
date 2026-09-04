"""Fixture 1C/CRM order adapters. Reuse contract shapes; do not import live adapters or mutate global catalogs."""

from __future__ import annotations

import uuid

from governed_publish.contracts import MODE_FIXTURE


class FixtureOneCOrderAdapter:
    """Mirrors OneC document_create contract (integrations.onec.fixture_adapter) without catalog mutation."""

    SUPPORTED = frozenset({"CREATE_ORDER", "UPDATE_ORDER", "CANCEL_ORDER"})

    def execute(self, *, action: str, payload: dict) -> dict:
        if action not in self.SUPPORTED:
            return {"status": "UNSUPPORTED", "live": False, "mode": MODE_FIXTURE}
        return {
            "status": "EXECUTED_FIXTURE",
            "fixture_reference": f"fixture:onec:{action.lower()}:{uuid.uuid4().hex[:12]}",
            "mode": MODE_FIXTURE,
            "live": False,
            "operation": "document_create" if action != "CANCEL_ORDER" else "document_create",
            "capability": "erp.1c.catalog.write",
            "panda_order_id": payload.get("order_id"),
            "external_order_id": payload.get("external_order_id"),
        }


class FixtureCrmOrderAdapter:
    """CRM deal create is unsupported in existing fixture adapter; contact/activity refs are supported."""

    def execute(self, *, action: str, payload: dict) -> dict:
        if action == "CREATE_OR_UPDATE_DEAL":
            return {"status": "UNSUPPORTED", "live": False, "mode": MODE_FIXTURE, "reason": "crm_deal_write_not_in_adapter"}
        if action in {"LINK_CUSTOMER_REFERENCE", "ADD_ORDER_REFERENCE", "UPDATE_DEAL_STATUS"}:
            if action == "UPDATE_DEAL_STATUS":
                return {"status": "UNSUPPORTED", "live": False, "mode": MODE_FIXTURE, "reason": "crm_deal_write_not_in_adapter"}
            op = "contact_create" if action == "LINK_CUSTOMER_REFERENCE" else "activity_create"
            return {
                "status": "EXECUTED_FIXTURE",
                "fixture_reference": f"fixture:crm:{op}:{uuid.uuid4().hex[:12]}",
                "mode": MODE_FIXTURE,
                "live": False,
                "operation": op,
                "capability": "crm.contact.write" if op == "contact_create" else "crm.activity.write",
                "customer_ref": payload.get("customer_ref"),
                "panda_order_id": payload.get("order_id"),
            }
        return {"status": "UNSUPPORTED", "live": False, "mode": MODE_FIXTURE}
