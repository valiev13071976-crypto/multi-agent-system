"""Unit tests for procurement policy snapshots."""

from __future__ import annotations

import unittest

from procurement.models import PROCUREMENT_POLICY_VERSION, PROCUREMENT_SCORING_VERSION
from procurement.policy import ProcurementPolicy, ProcurementScoringPolicy, procurement_policy_snapshot


class ProcurementPolicyTests(unittest.TestCase):
    def test_policy_defaults(self):
        policy = ProcurementPolicy()
        self.assertTrue(policy.approval_required)
        self.assertTrue(policy.require_price_provenance)
        self.assertTrue(policy.exclude_restricted_suppliers)

    def test_snapshot_versions(self):
        snap = procurement_policy_snapshot()
        self.assertEqual(snap["procurement_policy_version"], PROCUREMENT_POLICY_VERSION)
        self.assertEqual(snap["procurement_scoring_version"], PROCUREMENT_SCORING_VERSION)
        self.assertTrue(snap["no_purchase_execution"])

    def test_scoring_weights_present(self):
        weights = ProcurementScoringPolicy().as_dict()["weights"]
        self.assertIn("spec_match", weights)
        self.assertIn("total_cost", weights)


if __name__ == "__main__":
    unittest.main()
