"""Persistence failure-path tests live primarily in test_side_effect_persistence_security.py.

This module keeps the P7D expected filename and covers DB-unavailable activation wiring.
"""

import unittest

from side_effects.errors import SideEffectPersistenceUnavailableError
from side_effects.persistence import build_side_effect_persistence


class SideEffectPersistenceFailureModuleTests(unittest.TestCase):

    def test_unavailable_bundle_reason(self):
        bundle = build_side_effect_persistence(
            durable=True,
            db_path="://invalid",
            run_recovery_scan=False,
        )
        self.assertFalse(bundle.ready)
        self.assertTrue(bool(bundle.reason_code))


if __name__ == "__main__":
    unittest.main()
