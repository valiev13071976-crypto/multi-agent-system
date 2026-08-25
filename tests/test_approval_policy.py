import unittest
from datetime import datetime, timedelta, timezone

from autonomy.capabilities import (
    CAP_EXTERNAL_READ,
    CAP_EXTERNAL_WRITE,
    CAP_MESSAGE_SEND,
    CAP_PERMISSION_MANAGE,
    CAP_PURCHASE,
    CapabilitySet,
)
from autonomy.gate import AutonomyGate, build_proposed_action
from autonomy.models import (
    ACTION_DELETE,
    ACTION_FINANCIAL_CHANGE,
    ACTION_PERMISSION_CHANGE,
    ACTION_PURCHASE,
    ACTION_READ,
    ACTION_SEND_MESSAGE,
    ACTION_WRITE,
    APPROVAL_APPROVED,
    APPROVAL_EXPIRED,
    APPROVAL_REJECTED,
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
    DECISION_REVIEW_AFTER,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    ApprovalRecord,
    utc_now,
)
from autonomy.risk import ActionRiskClassifier
from tools.models import (
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_PRIVILEGED,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
)
from workflow.engine import WorkflowEngine
from workflow.models import STATUS_RUNNING, STATUS_WAITING_APPROVAL


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def caps(*names):
    return CapabilitySet(subject_id="agent-1", capabilities=tuple(names), issued_at=T0)


class RiskClassifierTests(unittest.TestCase):

    def setUp(self):
        self.classifier = ActionRiskClassifier()

    def test_i_read_is_low(self):
        self.assertEqual(self.classifier.classify(ACTION_READ), RISK_LOW)

    def test_j_write_at_least_medium(self):
        risk = self.classifier.classify(ACTION_WRITE, metadata={"reversible": True})
        self.assertIn(risk, {RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL})
        self.assertNotEqual(risk, RISK_LOW)

    def test_k_send_message_is_high(self):
        self.assertEqual(self.classifier.classify(ACTION_SEND_MESSAGE), RISK_HIGH)

    def test_l_purchase_is_critical(self):
        self.assertEqual(self.classifier.classify(ACTION_PURCHASE), RISK_CRITICAL)

    def test_m_financial_change_is_critical(self):
        self.assertEqual(self.classifier.classify(ACTION_FINANCIAL_CHANGE), RISK_CRITICAL)

    def test_n_permission_change_is_critical(self):
        self.assertEqual(self.classifier.classify(ACTION_PERMISSION_CHANGE), RISK_CRITICAL)

    def test_o_unknown_destructive_is_not_low(self):
        risk = self.classifier.classify(
            "explode",
            operation="destroy",
            metadata={"unknown": True, "destructive": True},
        )
        self.assertNotEqual(risk, RISK_LOW)
        self.assertEqual(risk, RISK_CRITICAL)
        delete_risk = self.classifier.classify(ACTION_DELETE)
        self.assertNotEqual(delete_risk, RISK_LOW)


class ApprovalPolicyTests(unittest.TestCase):

    def test_p_advisor_read_no_side_effect(self):
        gate = AutonomyGate(autonomy_level="advisor")
        action = build_proposed_action(action_type="read")
        decision = gate.evaluate(action, capabilities=caps(CAP_EXTERNAL_READ))
        self.assertEqual(decision.decision, DECISION_ALLOW)
        write = build_proposed_action(
            action_type="write",
            tool_id="notes",
            operation="update",
            resource="meta",
            tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            idempotency_key="k1",
            metadata={"reversible": True},
        )
        denied = gate.evaluate(write, capabilities=caps(CAP_EXTERNAL_WRITE))
        self.assertEqual(denied.decision, DECISION_DENY)

    def test_q_advisor_write_denies(self):
        gate = AutonomyGate(autonomy_level="advisor")
        action = build_proposed_action(
            action_type="write",
            idempotency_key="k",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        decision = gate.evaluate(action, capabilities=caps(CAP_EXTERNAL_WRITE))
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "advisor_side_effect_denied")

    def test_r_analyst_external_read_allows(self):
        gate = AutonomyGate(autonomy_level="analyst")
        action = build_proposed_action(action_type="read")
        decision = gate.evaluate(action, capabilities=caps(CAP_EXTERNAL_READ))
        self.assertEqual(decision.decision, DECISION_ALLOW)

    def test_s_analyst_write_denies(self):
        gate = AutonomyGate(autonomy_level="analyst")
        action = build_proposed_action(
            action_type="write",
            idempotency_key="k",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        decision = gate.evaluate(action, capabilities=caps(CAP_EXTERNAL_WRITE))
        self.assertEqual(decision.decision, DECISION_DENY)

    def test_t_executor_confirmed_reversible_write_requires_approval(self):
        gate = AutonomyGate(autonomy_level="executor_confirmed")
        action = build_proposed_action(
            action_type="write",
            idempotency_key="k-t",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        decision = gate.evaluate(action, capabilities=caps(CAP_EXTERNAL_WRITE))
        self.assertEqual(decision.decision, DECISION_REQUIRE_APPROVAL)

    def test_u_executor_bounded_low_reversible_scoped(self):
        gate = AutonomyGate(autonomy_level="executor_bounded")
        action = build_proposed_action(
            action_type="write",
            tool_id="notes",
            operation="patch",
            resource="internal",
            idempotency_key="k-u",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            risk_class=RISK_LOW,
        )
        decision = gate.evaluate(action, capabilities=caps(CAP_EXTERNAL_WRITE))
        self.assertIn(decision.decision, {DECISION_ALLOW, DECISION_REVIEW_AFTER})

    def test_v_executor_bounded_high_requires_approval(self):
        gate = AutonomyGate(autonomy_level="executor_bounded")
        action = build_proposed_action(
            action_type="write",
            idempotency_key="k-v",
            metadata={"reversible": False},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            risk_class=RISK_HIGH,
        )
        decision = gate.evaluate(action, capabilities=caps(CAP_EXTERNAL_WRITE))
        self.assertEqual(decision.decision, DECISION_REQUIRE_APPROVAL)

    def test_w_critical_purchase_never_auto_allows(self):
        gate = AutonomyGate(autonomy_level="executor_bounded")
        action = build_proposed_action(
            action_type="purchase",
            tool_id="shop",
            operation="buy",
            resource="sku",
            idempotency_key="k-w",
            requested_capabilities=(CAP_PURCHASE,),
            tool_trust_level=TOOL_TRUST_PRIVILEGED,
        )
        decision = gate.evaluate(action, capabilities=caps(CAP_PURCHASE))
        self.assertNotEqual(decision.decision, DECISION_ALLOW)
        self.assertEqual(decision.decision, DECISION_REQUIRE_APPROVAL)
        self.assertEqual(action.risk_class, RISK_CRITICAL)

    def test_x_customer_message_never_auto_allows(self):
        gate = AutonomyGate(autonomy_level="executor_bounded")
        action = build_proposed_action(
            action_type="send_message",
            tool_id="telegram",
            operation="send",
            resource="customer",
            idempotency_key="k-x",
            requested_capabilities=(CAP_MESSAGE_SEND,),
            tool_trust_level="WRITE_EXTERNAL_IRREVERSIBLE",
        )
        decision = gate.evaluate(action, capabilities=caps(CAP_MESSAGE_SEND))
        self.assertNotEqual(decision.decision, DECISION_ALLOW)
        self.assertEqual(decision.decision, DECISION_REQUIRE_APPROVAL)

    def test_y_permission_change_never_auto_allows(self):
        gate = AutonomyGate(autonomy_level="executor_bounded")
        action = build_proposed_action(
            action_type="permission_change",
            tool_id="iam",
            operation="grant",
            resource="role",
            idempotency_key="k-y",
            requested_capabilities=(CAP_PERMISSION_MANAGE,),
            tool_trust_level=TOOL_TRUST_PRIVILEGED,
        )
        decision = gate.evaluate(action, capabilities=caps(CAP_PERMISSION_MANAGE))
        self.assertNotEqual(decision.decision, DECISION_ALLOW)

    def test_z_unknown_tool_trust_denies(self):
        gate = AutonomyGate(autonomy_level="analyst")
        action = build_proposed_action(
            action_type="read",
            tool_trust_level="mystery",
        )
        decision = gate.evaluate(action, capabilities=caps(CAP_EXTERNAL_READ))
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "unknown_tool_trust")

    def _running_engine(self):
        engine = WorkflowEngine()
        workflow_id = engine.create("task-appr")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        return engine, workflow_id

    def test_ag_require_approval_sets_waiting_approval(self):
        engine, workflow_id = self._running_engine()
        action = build_proposed_action(
            action_type="write",
            workflow_id=workflow_id,
            task_id="task-appr",
            idempotency_key="k-ag",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        decision = engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        self.assertEqual(decision.decision, DECISION_REQUIRE_APPROVAL)
        state = engine.state_manager.get(workflow_id)
        self.assertEqual(state.status, STATUS_WAITING_APPROVAL)
        point = engine.state_manager.get_checkpoint(workflow_id)
        self.assertEqual(point.payload["action_id"], action.action_id)
        self.assertEqual(point.payload["decision_id"], decision.decision_id)
        self.assertTrue(point.payload["required_approval"])
        self.assertNotIn("prompt", point.payload)

    def test_ah_approval_approved_may_resume(self):
        engine, workflow_id = self._running_engine()
        action = build_proposed_action(
            action_type="write",
            workflow_id=workflow_id,
            task_id="task-appr",
            idempotency_key="k-ah",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        decision = engine.resolve_action_approval(
            engine.last_approval_id,
            APPROVAL_APPROVED,
            approved_by="reviewer-1",
            action=action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        self.assertEqual(decision.decision, DECISION_ALLOW)
        self.assertEqual(
            engine.state_manager.get(workflow_id).status, STATUS_RUNNING
        )

    def test_ai_approval_rejected_does_not_execute(self):
        engine, workflow_id = self._running_engine()
        action = build_proposed_action(
            action_type="write",
            workflow_id=workflow_id,
            task_id="task-appr",
            idempotency_key="k-ai",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        decision = engine.resolve_action_approval(
            engine.last_approval_id,
            APPROVAL_REJECTED,
            approved_by="reviewer-1",
            action=action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertNotEqual(decision.decision, DECISION_ALLOW)

    def test_aj_approval_expired_does_not_execute(self):
        engine, workflow_id = self._running_engine()
        action = build_proposed_action(
            action_type="write",
            workflow_id=workflow_id,
            task_id="task-appr",
            idempotency_key="k-aj",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        decision = engine.resolve_action_approval(
            engine.last_approval_id,
            APPROVAL_EXPIRED,
            approved_by="system",
            action=action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        self.assertEqual(decision.decision, DECISION_DENY)

    def test_ak_approval_does_not_override_missing_capability(self):
        engine, workflow_id = self._running_engine()
        action = build_proposed_action(
            action_type="write",
            workflow_id=workflow_id,
            task_id="task-appr",
            idempotency_key="k-ak",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        decision = engine.resolve_action_approval(
            engine.last_approval_id,
            APPROVAL_APPROVED,
            approved_by="reviewer-1",
            action=action,
            capabilities=caps(CAP_EXTERNAL_READ),
            autonomy_level="executor_confirmed",
        )
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "capability_missing")

    def test_al_approval_does_not_override_expired_token(self):
        from autonomy.tokens import CapabilityToken, HmacSha256TokenSigner, sign_token
        from autonomy.capabilities import CapabilityScope

        signer = HmacSha256TokenSigner(key=b"p5a-unit-hmac-sha256-signing-key")
        engine, workflow_id = self._running_engine()
        engine.autonomy_gate = AutonomyGate(signer=signer)
        token = sign_token(
            CapabilityToken(
                token_id="tok-al",
                subject_id="agent-1",
                capabilities=(CAP_EXTERNAL_WRITE,),
                scope=CapabilityScope(),
                issued_at=T0,
                expires_at=T0 + timedelta(hours=1),
                nonce="n",
            ),
            signer,
        )
        action = build_proposed_action(
            action_type="write",
            workflow_id=workflow_id,
            task_id="task-appr",
            idempotency_key="k-al",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        engine.evaluate_action(
            action,
            token=token,
            autonomy_level="executor_confirmed",
            now=T0,
        )
        expired = sign_token(
            CapabilityToken(
                token_id="tok-al-exp",
                subject_id="agent-1",
                capabilities=(CAP_EXTERNAL_WRITE,),
                scope=CapabilityScope(),
                issued_at=T0,
                expires_at=T0,
                nonce="n2",
            ),
            signer,
        )
        decision = engine.resolve_action_approval(
            engine.last_approval_id,
            APPROVAL_APPROVED,
            approved_by="reviewer-1",
            action=action,
            token=expired,
            autonomy_level="executor_confirmed",
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "token_expired")

    def test_am_after_approval_gate_reevaluates(self):
        engine, workflow_id = self._running_engine()
        action = build_proposed_action(
            action_type="write",
            workflow_id=workflow_id,
            task_id="task-appr",
            idempotency_key="k-am",
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        first = engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        self.assertEqual(first.decision, DECISION_REQUIRE_APPROVAL)
        second = engine.resolve_action_approval(
            engine.last_approval_id,
            APPROVAL_APPROVED,
            approved_by="reviewer-1",
            action=action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        self.assertEqual(second.reason_code, "approved_reevaluated")
        self.assertNotEqual(first.decision_id, second.decision_id)


if __name__ == "__main__":
    unittest.main()

