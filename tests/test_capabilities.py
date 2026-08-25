from datetime import datetime, timedelta, timezone
import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilityScope, CapabilitySet
from autonomy.gate import AutonomyGate, build_proposed_action
from autonomy.models import DECISION_ALLOW, DECISION_DENY
from autonomy.tokens import (
    CapabilityToken,
    HmacSha256TokenSigner,
    sign_token,
)


SIGN_KEY = b"p5a-unit-hmac-sha256-signing-key"
T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def signed_token(**overrides):
    fields = {
        "token_id": str(uuid.uuid4()),
        "subject_id": "agent-1",
        "capabilities": (CAP_EXTERNAL_READ,),
        "scope": CapabilityScope(),
        "issued_at": T0,
        "expires_at": T0 + timedelta(hours=1),
        "nonce": "nonce-1",
        "workflow_id": "wf-1",
        "task_id": "task-1",
    }
    fields.update(overrides)
    token = CapabilityToken(**fields)
    return sign_token(token, HmacSha256TokenSigner(key=SIGN_KEY))


class CapabilityTests(unittest.TestCase):

    def setUp(self):
        self.signer = HmacSha256TokenSigner(key=SIGN_KEY)
        self.gate = AutonomyGate(signer=self.signer, autonomy_level="analyst")

    def test_a_missing_capability_denies(self):
        action = build_proposed_action(action_type="read")
        caps = CapabilitySet(
            subject_id="agent-1",
            capabilities=(),
            issued_at=T0,
        )
        decision = self.gate.evaluate(action, capabilities=caps)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "capability_missing")

    def test_b_valid_capability_passes_check(self):
        action = build_proposed_action(action_type="read")
        caps = CapabilitySet(
            subject_id="agent-1",
            capabilities=(CAP_EXTERNAL_READ,),
            issued_at=T0,
        )
        decision = self.gate.evaluate(action, capabilities=caps)
        self.assertEqual(decision.decision, DECISION_ALLOW)
        self.assertEqual(decision.capabilities_checked, (CAP_EXTERNAL_READ,))

    def test_c_expired_token_denies(self):
        action = build_proposed_action(action_type="read")
        token = signed_token(expires_at=T0 - timedelta(seconds=1))
        decision = self.gate.evaluate(action, token=token, now=T0)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "token_expired")

    def test_d_wrong_workflow_scope_denies(self):
        action = build_proposed_action(action_type="read", workflow_id="wf-other")
        token = signed_token(
            scope=CapabilityScope(workflow_id="wf-1"),
            workflow_id="wf-1",
        )
        decision = self.gate.evaluate(action, token=token, now=T0)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "scope_workflow_mismatch")

    def test_e_wrong_task_scope_denies(self):
        action = build_proposed_action(action_type="read", task_id="task-other")
        token = signed_token(
            scope=CapabilityScope(task_id="task-1"),
            task_id="task-1",
        )
        decision = self.gate.evaluate(action, token=token, now=T0)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "scope_task_mismatch")

    def test_f_wrong_tool_scope_denies(self):
        action = build_proposed_action(action_type="read", tool_id="search")
        token = signed_token(scope=CapabilityScope(tool_id="crm"))
        decision = self.gate.evaluate(action, token=token, now=T0)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "scope_tool_mismatch")

    def test_g_wrong_operation_scope_denies(self):
        action = build_proposed_action(action_type="read", operation="search")
        token = signed_token(scope=CapabilityScope(operation="update_lead"))
        decision = self.gate.evaluate(action, token=token, now=T0)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "scope_operation_mismatch")

    def test_h_raw_signature_not_in_decision_metadata(self):
        action = build_proposed_action(action_type="read")
        token = signed_token()
        decision = self.gate.evaluate(action, token=token, now=T0)
        blob = str(dict(decision.metadata))
        self.assertNotIn(token.signature, blob)
        self.assertNotIn(SIGN_KEY.decode(), blob)
        self.assertEqual(decision.metadata["token_claims"]["token_id"], token.token.token_id)
        self.assertNotIn("signature", decision.metadata["token_claims"])


if __name__ == "__main__":
    unittest.main()
