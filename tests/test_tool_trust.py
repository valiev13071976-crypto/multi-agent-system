from datetime import datetime, timezone
import unittest

from autonomy.gate import AutonomyGate, build_proposed_action
from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
from autonomy.models import DECISION_DENY
from tools.gateway import ToolGateway
from tools.models import (
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_LEVELS,
    TOOL_TRUST_PRIVILEGED,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
)
from tools.trust import SEARCH_TOOL_TRUST

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class ToolTrustTests(unittest.TestCase):

    def test_an_search_tool_is_read_only_external(self):
        self.assertEqual(SEARCH_TOOL_TRUST, TOOL_TRUST_READ_ONLY_EXTERNAL)
        gateway = ToolGateway()
        self.assertEqual(gateway.tool_trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)

    def test_ao_write_reversible_trust(self):
        self.assertEqual(
            TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            "WRITE_EXTERNAL_REVERSIBLE",
        )
        self.assertIn(TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, TOOL_TRUST_LEVELS)

    def test_ap_irreversible_external_trust(self):
        self.assertEqual(
            TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
            "WRITE_EXTERNAL_IRREVERSIBLE",
        )

    def test_aq_privileged_trust(self):
        self.assertEqual(TOOL_TRUST_PRIVILEGED, "PRIVILEGED")
        self.assertIn(TOOL_TRUST_INTERNAL_SAFE, TOOL_TRUST_LEVELS)

    def test_ar_unknown_trust_denies(self):
        gate = AutonomyGate(autonomy_level="analyst")
        action = build_proposed_action(action_type="read", tool_trust_level="nope")
        caps = CapabilitySet(
            subject_id="s",
            capabilities=(CAP_EXTERNAL_READ,),
            issued_at=T0,
        )
        decision = gate.evaluate(action, capabilities=caps)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "unknown_tool_trust")


if __name__ == "__main__":
    unittest.main()
