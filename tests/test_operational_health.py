import unittest

from observability.health import HEALTH_BLOCKED, HEALTH_DEGRADED, HEALTH_HEALTHY
from observability.health import build_operational_health


class OperationalHealthStandaloneTests(unittest.TestCase):
    def test_pending_reconciliation_degraded(self):
        snap = build_operational_health(pending_reconciliations=2)
        self.assertEqual(snap.reconciliation_status, HEALTH_DEGRADED)
        self.assertEqual(snap.overall_status, HEALTH_DEGRADED)

    def test_uncertain_side_effects_degraded(self):
        snap = build_operational_health(uncertain_side_effects=1)
        self.assertEqual(snap.overall_status, HEALTH_DEGRADED)

    def test_blocked_overrides_degraded(self):
        snap = build_operational_health(
            uncertain_side_effects=1,
            protected_state_ready=False,
            protected_write_required=True,
        )
        self.assertEqual(snap.overall_status, HEALTH_BLOCKED)
        _ = HEALTH_HEALTHY


if __name__ == "__main__":
    unittest.main()
