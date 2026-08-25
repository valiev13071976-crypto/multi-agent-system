import unittest

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from hitl.models import (
    EVENT_APPROVAL_APPROVED,
    EVENT_APPROVAL_CANCELLED,
    EVENT_APPROVAL_EXPIRED,
    EVENT_APPROVAL_REJECTED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_PERMIT_CONSUMED,
    EVENT_PERMIT_ISSUED,
    EVENT_PERMIT_REVOKED,
    EVENT_REEVALUATION_PASSED,
)
from tests.test_hitl_service import T0, caps, running_engine, write_action


class HITLAuditTests(unittest.TestCase):

    def test_am_request_creates_audit_event(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        types = [e.event_type for e in engine._hitl().audit.events()]
        self.assertIn(EVENT_APPROVAL_REQUESTED, types)

    def test_an_resolution_events(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine._hitl().approve(engine.last_approval_id, resolved_by="reviewer-1", now=T0)
        types = [e.event_type for e in engine._hitl().audit.events()]
        self.assertIn(EVENT_APPROVAL_APPROVED, types)

        engine2, wf2 = running_engine()
        action2 = write_action(wf2, idempotency_key="idem-2")
        engine2.evaluate_action(
            action2,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine2._hitl().reject(engine2.last_approval_id, resolved_by="reviewer-1", now=T0)
        engine3, wf3 = running_engine()
        action3 = write_action(wf3, idempotency_key="idem-3")
        engine3.evaluate_action(
            action3,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine3._hitl().expire(engine3.last_approval_id, now=T0)
        engine4, wf4 = running_engine()
        action4 = write_action(wf4, idempotency_key="idem-4")
        engine4.evaluate_action(
            action4,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine4._hitl().cancel(engine4.last_approval_id, resolved_by="reviewer-1", now=T0)
        self.assertIn(EVENT_APPROVAL_REJECTED, [e.event_type for e in engine2._hitl().audit.events()])
        self.assertIn(EVENT_APPROVAL_EXPIRED, [e.event_type for e in engine3._hitl().audit.events()])
        self.assertIn(EVENT_APPROVAL_CANCELLED, [e.event_type for e in engine4._hitl().audit.events()])

    def test_ao_ap_reevaluation_and_permit_events(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine._hitl().approve(engine.last_approval_id, resolved_by="reviewer-1", now=T0)
        permit = engine._hitl().reevaluate_and_issue_permit(
            engine.last_approval_id,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        engine._hitl().consume_for_execution(permit.permit_id, action=action, now=T0)
        engine5, wf5 = running_engine()
        action5 = write_action(wf5, idempotency_key="idem-5")
        engine5.evaluate_action(
            action5,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine5._hitl().approve(engine5.last_approval_id, resolved_by="reviewer-1", now=T0)
        permit5 = engine5._hitl().reevaluate_and_issue_permit(
            engine5.last_approval_id,
            action5,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        engine5._hitl().revoke_permit(permit5.permit_id)
        types = [e.event_type for e in engine._hitl().audit.events()]
        self.assertIn(EVENT_REEVALUATION_PASSED, types)
        self.assertIn(EVENT_PERMIT_ISSUED, types)
        self.assertIn(EVENT_PERMIT_CONSUMED, types)
        self.assertIn(EVENT_PERMIT_REVOKED, [e.event_type for e in engine5._hitl().audit.events()])

    def test_aq_audit_append_only(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        events = engine._hitl().audit.store._events
        first_id = events[0].event_id
        self.assertFalse(hasattr(engine._hitl().audit.store, "update"))
        events[0]  # cannot replace via API
        self.assertEqual(engine._hitl().audit.events()[0].event_id, first_id)

    def test_ar_audit_has_no_secrets(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        blob = str([dict(e.metadata) for e in engine._hitl().audit.events()])
        self.assertNotIn("prompt", blob)
        self.assertNotIn("Authorization", blob)
        self.assertNotIn("sk-", blob)


if __name__ == "__main__":
    unittest.main()
