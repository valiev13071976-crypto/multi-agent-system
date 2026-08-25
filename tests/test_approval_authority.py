import unittest

from hitl.authority import (
    InMemoryApprovalAuthority,
    ROLE_CRITICAL_APPROVER,
    ROLE_HIGH_RISK_APPROVER,
    ROLE_PRIVILEGED_APPROVER,
    ROLE_STANDARD_APPROVER,
)
from hitl.models import (
    APPROVAL_CLASS_CRITICAL,
    APPROVAL_CLASS_HIGH_RISK,
    APPROVAL_CLASS_PRIVILEGED,
    APPROVAL_CLASS_STANDARD,
)


class ApprovalAuthorityTests(unittest.TestCase):

    def test_ah_standard_can_standard(self):
        auth = InMemoryApprovalAuthority()
        auth.grant("s", ROLE_STANDARD_APPROVER)
        self.assertTrue(auth.can_resolve("s", APPROVAL_CLASS_STANDARD))

    def test_ai_standard_cannot_critical(self):
        auth = InMemoryApprovalAuthority()
        auth.grant("s", ROLE_STANDARD_APPROVER)
        self.assertFalse(auth.can_resolve("s", APPROVAL_CLASS_CRITICAL))
        self.assertFalse(auth.can_resolve("s", APPROVAL_CLASS_PRIVILEGED))

    def test_aj_critical_can_lower_and_critical(self):
        auth = InMemoryApprovalAuthority()
        auth.grant("c", ROLE_CRITICAL_APPROVER)
        self.assertTrue(auth.can_resolve("c", APPROVAL_CLASS_STANDARD))
        self.assertTrue(auth.can_resolve("c", APPROVAL_CLASS_HIGH_RISK))
        self.assertTrue(auth.can_resolve("c", APPROVAL_CLASS_CRITICAL))
        self.assertFalse(auth.can_resolve("c", APPROVAL_CLASS_PRIVILEGED))

    def test_ak_privileged_can_all(self):
        auth = InMemoryApprovalAuthority()
        auth.grant("p", ROLE_PRIVILEGED_APPROVER)
        for cls in (
            APPROVAL_CLASS_STANDARD,
            APPROVAL_CLASS_HIGH_RISK,
            APPROVAL_CLASS_CRITICAL,
            APPROVAL_CLASS_PRIVILEGED,
        ):
            self.assertTrue(auth.can_resolve("p", cls))

    def test_al_unknown_subject_denied(self):
        auth = InMemoryApprovalAuthority()
        self.assertFalse(auth.can_resolve("nobody", APPROVAL_CLASS_STANDARD))
        self.assertFalse(auth.can_resolve("", APPROVAL_CLASS_STANDARD))
        auth.grant("h", ROLE_HIGH_RISK_APPROVER)
        self.assertTrue(auth.can_resolve("h", APPROVAL_CLASS_HIGH_RISK))
        self.assertFalse(auth.can_resolve("h", APPROVAL_CLASS_CRITICAL))


if __name__ == "__main__":
    unittest.main()
