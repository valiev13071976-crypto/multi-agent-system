from datetime import datetime, timedelta, timezone
import unittest

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from hitl.errors import (
    ExecutionPermitConsumedError,
    ExecutionPermitExpiredError,
    ExecutionPermitMismatchError,
    ExecutionPermitRevokedError,
)
from hitl.models import PERMIT_ISSUED, ExecutionPermit
from hitl.permit import PermitService
from tests.test_hitl_service import T0, caps, running_engine, write_action


class ExecutionPermitTests(unittest.TestCase):

    def _permit(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine._hitl().approve(
            engine.last_approval_id, resolved_by="reviewer-1", now=T0
        )
        permit = engine._hitl().reevaluate_and_issue_permit(
            engine.last_approval_id,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        return engine, action, permit

    def test_w_successful_reevaluation_one_permit(self):
        engine, action, permit = self._permit()
        self.assertEqual(permit.status, PERMIT_ISSUED)
        self.assertTrue(permit.single_use)

    def test_x_permit_has_no_raw_token_or_secret(self):
        _, _, permit = self._permit()
        blob = str(permit.public_view()) + str(dict(permit.metadata))
        self.assertNotIn("signature", blob)
        self.assertNotIn("PANDA", blob)
        self.assertNotIn("Bearer", blob)

    def test_y_expired_permit_rejected(self):
        engine, action, permit = self._permit()
        with self.assertRaises(ExecutionPermitExpiredError):
            engine._hitl().permits.validate(
                permit, action=action, now=T0 + timedelta(seconds=301)
            )

    def test_z_wrong_action_id_rejected(self):
        engine, action, permit = self._permit()
        other = write_action(action.workflow_id, action_id="other-action")
        with self.assertRaises(ExecutionPermitMismatchError):
            engine._hitl().permits.validate(permit, action=other, now=T0)

    def test_aa_wrong_workflow_task_rejected(self):
        engine, action, permit = self._permit()
        other = write_action("wf-x", task_id="other", action_id=action.action_id)
        with self.assertRaises(ExecutionPermitMismatchError):
            engine._hitl().permits.validate(permit, action=other, now=T0)

    def test_ab_wrong_fingerprint_rejected(self):
        engine, action, permit = self._permit()
        other = write_action(
            action.workflow_id,
            action_id=action.action_id,
            tool_id="other-tool",
        )
        with self.assertRaises(ExecutionPermitMismatchError):
            engine._hitl().permits.validate(permit, action=other, now=T0)

    def test_ac_wrong_idempotency_key_rejected(self):
        engine, action, permit = self._permit()
        other = write_action(
            action.workflow_id,
            action_id=action.action_id,
            idempotency_key="other-key",
        )
        with self.assertRaises(ExecutionPermitMismatchError):
            engine._hitl().permits.validate(permit, action=other, now=T0)

    def test_ad_consume_once(self):
        engine, action, permit = self._permit()
        consumed = engine._hitl().consume_for_execution(permit.permit_id, action=action, now=T0)
        self.assertEqual(consumed.status, "consumed")

    def test_ae_consume_twice(self):
        engine, action, permit = self._permit()
        engine._hitl().consume_for_execution(permit.permit_id, action=action, now=T0)
        with self.assertRaises(ExecutionPermitConsumedError):
            engine._hitl().consume_for_execution(permit.permit_id, action=action, now=T0)

    def test_af_revoked_permit_rejected(self):
        engine, action, permit = self._permit()
        engine._hitl().revoke_permit(permit.permit_id)
        revoked = engine._hitl().permits.get(permit.permit_id)
        with self.assertRaises(ExecutionPermitRevokedError):
            engine._hitl().permits.validate(revoked, action=action, now=T0)


if __name__ == "__main__":
    unittest.main()
