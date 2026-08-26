"""Unit tests for procurement access and security controls."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope
from procurement.access import ProcurementAccessDenied
from procurement.errors import PROCUREMENT_ACTION_DENIED, PROCUREMENT_SCOPE_DENIED, ProcurementError
from procurement.models import ProcurementRequest
from procurement.runtime import build_procurement_runtime


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class ProcurementSecurityTests(unittest.TestCase):
    def test_cross_scope_read_denied(self):
        rt = build_procurement_runtime(env={"PROCUREMENT_ENABLED": "true"})
        svc = rt.service
        scope_a = _scope("a")
        scope_b = _scope("b")
        svc.create_request(
            ProcurementRequest(
                request_id="r1",
                scope=scope_a,
                requested_by="user",
                item_name="Widget",
                quantity=Decimal("10"),
                unit="pcs",
            ),
            requesting_scope=scope_a,
        )
        with self.assertRaises(ProcurementError) as ctx:
            svc.get_request("r1", requesting_scope=scope_b)
        self.assertEqual(ctx.exception.reason, PROCUREMENT_SCOPE_DENIED)

    def test_financial_execution_denied(self):
        rt = build_procurement_runtime(env={"PROCUREMENT_ENABLED": "true"})
        with self.assertRaises(ProcurementError) as ctx:
            rt.service.execute_financial_action("pay_supplier")
        self.assertEqual(ctx.exception.reason, PROCUREMENT_ACTION_DENIED)

    def test_access_policy_requires_matching_scope(self):
        from procurement.access import ProcurementAccessPolicy

        policy = ProcurementAccessPolicy()
        with self.assertRaises(ProcurementAccessDenied):
            policy.require(requesting=_scope("a"), target=_scope("b"), operation="read")


if __name__ == "__main__":
    unittest.main()
