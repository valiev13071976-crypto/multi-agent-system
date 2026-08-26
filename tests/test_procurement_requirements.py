"""Unit tests for requirement normalization."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope
from procurement.models import ProcurementRequest
from procurement.normalizer import normalize_requirements


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class ProcurementRequirementsTests(unittest.TestCase):
    def test_incomplete_when_quantity_missing(self):
        req = ProcurementRequest(
            request_id="r1",
            scope=_scope(),
            requested_by="user",
            item_name="Widget",
            quantity=None,
            unit="pcs",
        )
        requirement = normalize_requirements(req)
        self.assertTrue(requirement.incomplete)
        self.assertIn("quantity", requirement.missing_fields)

    def test_mandatory_specs_from_request(self):
        req = ProcurementRequest(
            request_id="r1",
            scope=_scope(),
            requested_by="user",
            item_name="Widget",
            quantity=Decimal("10"),
            unit="pcs",
            specifications={"color": "blue", "preferred_finish": "matte"},
        )
        requirement = normalize_requirements(req)
        self.assertFalse(requirement.incomplete)
        self.assertEqual(requirement.mandatory_specs["color"], "blue")
        self.assertEqual(requirement.preferred_specs["preferred_finish"], "matte")


if __name__ == "__main__":
    unittest.main()
