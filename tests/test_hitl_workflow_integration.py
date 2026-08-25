from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock

from autonomy.capabilities import (
    CAP_EXTERNAL_READ,
    CAP_EXTERNAL_WRITE,
    CapabilityScope,
    CapabilitySet,
)
from autonomy.gate import AutonomyGate, build_proposed_action
from autonomy.models import DECISION_ALLOW, DECISION_DENY, IDEMPOTENCY_COMPLETED
from autonomy.tokens import CapabilityToken, HmacSha256TokenSigner, sign_token
from hitl.authority import InMemoryApprovalAuthority, ROLE_STANDARD_APPROVER
from hitl.errors import ActionIntegrityError
from hitl.models import APPROVAL_CLASS_CRITICAL, approval_class_for
from hitl.service import HITLService
from tests.test_hitl_service import T0, caps, running_engine, write_action
from tools.models import TOOL_TRUST_PRIVILEGED, TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE


SIGN_KEY = b"p5b-unit-hmac-sha256-signing-key"


class HITLReevaluationTests(unittest.TestCase):

    def _approved(self, engine, action, **kwargs):
        kwargs.setdefault("now", T0)
        engine.evaluate_action(
            action,
            requested_by="agent-1",
            **kwargs,
        )
        engine._hitl().approve(
            engine.last_approval_id, resolved_by="reviewer-1", now=T0
        )
        return engine.last_approval_id

    def test_o_approved_unchanged_issues_permit(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        approval_id = self._approved(
            engine,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        permit = engine._hitl().reevaluate_and_issue_permit(
            approval_id,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        self.assertIsNotNone(permit)
        self.assertEqual(permit.status, "issued")
        self.assertEqual(permit.action_id, action.action_id)

    def test_p_token_expired_while_pending_no_permit(self):
        signer = HmacSha256TokenSigner(key=SIGN_KEY)
        engine, workflow_id = running_engine()
        engine._gate().signer = signer
        engine.hitl_service.gate = engine._gate()
        token = sign_token(
            CapabilityToken(
                token_id="tok-p",
                subject_id="agent-1",
                capabilities=(CAP_EXTERNAL_WRITE,),
                scope=CapabilityScope(),
                issued_at=T0,
                expires_at=T0 + timedelta(minutes=5),
                nonce="n",
            ),
            signer,
        )
        action = write_action(workflow_id)
        approval_id = self._approved(
            engine,
            action,
            token=token,
            autonomy_level="executor_confirmed",
            now=T0,
        )
        expired = sign_token(
            CapabilityToken(
                token_id="tok-p2",
                subject_id="agent-1",
                capabilities=(CAP_EXTERNAL_WRITE,),
                scope=CapabilityScope(),
                issued_at=T0,
                expires_at=T0 + timedelta(minutes=5),
                nonce="n2",
            ),
            signer,
        )
        permit = engine._hitl().reevaluate_and_issue_permit(
            approval_id,
            action,
            token=expired,
            autonomy_level="executor_confirmed",
            now=T0 + timedelta(minutes=10),
        )
        self.assertIsNone(permit)
        self.assertEqual(engine._hitl().last_reevaluation.decision, DECISION_DENY)
        self.assertEqual(engine._hitl().last_reevaluation.reason_code, "token_expired")

    def test_q_capability_removed_no_permit(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        approval_id = self._approved(
            engine,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        permit = engine._hitl().reevaluate_and_issue_permit(
            approval_id,
            action,
            capabilities=caps(CAP_EXTERNAL_READ),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        self.assertIsNone(permit)
        self.assertEqual(engine._hitl().last_reevaluation.reason_code, "capability_missing")

    def test_r_scope_mismatch_no_permit(self):
        signer = HmacSha256TokenSigner(key=SIGN_KEY)
        engine, workflow_id = running_engine()
        engine._gate().signer = signer
        token = sign_token(
            CapabilityToken(
                token_id="tok-r",
                subject_id="agent-1",
                capabilities=(CAP_EXTERNAL_WRITE,),
                scope=CapabilityScope(workflow_id=workflow_id),
                issued_at=T0,
                expires_at=T0 + timedelta(hours=1),
                nonce="n",
                workflow_id=workflow_id,
            ),
            signer,
        )
        action = write_action(workflow_id)
        approval_id = self._approved(
            engine,
            action,
            token=token,
            autonomy_level="executor_confirmed",
            now=T0,
        )
        other = write_action("wf-other", action_id=action.action_id, idempotency_key="idem-h")
        # fingerprint/workflow will fail class or integrity; use same fingerprint fields except workflow in token
        permit = engine._hitl().reevaluate_and_issue_permit(
            approval_id,
            action,
            token=sign_token(
                CapabilityToken(
                    token_id="tok-r2",
                    subject_id="agent-1",
                    capabilities=(CAP_EXTERNAL_WRITE,),
                    scope=CapabilityScope(workflow_id="wf-other"),
                    issued_at=T0,
                    expires_at=T0 + timedelta(hours=1),
                    nonce="n3",
                    workflow_id="wf-other",
                ),
                signer,
            ),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        self.assertIsNone(permit)

    def test_s_changed_tool_fingerprint(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        approval_id = self._approved(
            engine,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        changed = write_action(workflow_id, tool_id="crm", action_id=action.action_id)
        with self.assertRaises(ActionIntegrityError):
            engine._hitl().reevaluate_and_issue_permit(
                approval_id,
                changed,
                capabilities=caps(CAP_EXTERNAL_WRITE),
                autonomy_level="executor_confirmed",
                now=T0,
            )

    def test_t_risk_escalation_class_insufficient(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        approval_id = self._approved(
            engine,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        escalated = write_action(
            workflow_id,
            action_id=action.action_id,
            risk_class="critical",
        )
        # keep fingerprint fields except risk/trust — class check runs first
        permit = engine._hitl().reevaluate_and_issue_permit(
            approval_id,
            escalated,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        self.assertIsNone(permit)
        events = [e.reason_code for e in engine._hitl().audit.events()]
        self.assertIn("approval_class_insufficient", events)
        self.assertEqual(approval_class_for(escalated), APPROVAL_CLASS_CRITICAL)

    def test_u_completed_idempotency_no_permit(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        approval_id = self._approved(
            engine,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        record = engine._gate().idempotency.get(action.idempotency_key)
        if record is None:
            engine._gate().idempotency.reserve(action.idempotency_key, action.action_id)
        engine._gate().idempotency.mark_completed(action.idempotency_key)
        permit = engine._hitl().reevaluate_and_issue_permit(
            approval_id,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        self.assertIsNone(permit)

    def test_v_approval_never_bypasses_deny(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        approval_id = self._approved(
            engine,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        permit = engine._hitl().reevaluate_and_issue_permit(
            approval_id,
            action,
            capabilities=caps(),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        self.assertIsNone(permit)
        self.assertEqual(engine._hitl().last_reevaluation.decision, DECISION_DENY)

    def test_ag_one_active_permit_only(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        approval_id = self._approved(
            engine,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
        )
        first = engine._hitl().reevaluate_and_issue_permit(
            approval_id,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        second = engine._hitl().reevaluate_and_issue_permit(
            approval_id,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        self.assertEqual(first.permit_id, second.permit_id)


class HITLWorkflowQueueRegressionTests(unittest.IsolatedAsyncioTestCase):

    async def test_as_resume_before_decision_no_execution(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        ran = []

        async def one():
            ran.append("one")

        result = await engine.resume(workflow_id, handlers={"one": one})
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "waiting_approval")
        self.assertFalse(result["ready_for_execution"])
        self.assertEqual(ran, [])

    async def test_at_approved_reeval_running(self):
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
        with mock.patch("hitl.permit.utc_now", return_value=T0):
            result = await engine.resume(
                workflow_id, execution_permit=permit, action=action
            )
        self.assertTrue(result["ready_for_execution"])
        self.assertEqual(result["permit_id"], permit.permit_id)
        from workflow.models import STATUS_RUNNING

        self.assertEqual(engine.state_manager.get(workflow_id).status, STATUS_RUNNING)

    def test_au_rejected_failed(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine._hitl().reject(engine.last_approval_id, resolved_by="reviewer-1", now=T0)
        from workflow.models import STATUS_FAILED

        self.assertEqual(engine.state_manager.get(workflow_id).status, STATUS_FAILED)

    def test_av_expired_failed(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine._hitl().expire(engine.last_approval_id, now=T0)
        from workflow.models import STATUS_FAILED

        self.assertEqual(engine.state_manager.get(workflow_id).status, STATUS_FAILED)

    def test_aw_cancelled_cancelled(self):
        engine, workflow_id = running_engine()
        action = write_action(workflow_id)
        engine.evaluate_action(
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            requested_by="agent-1",
            now=T0,
        )
        engine._hitl().cancel(engine.last_approval_id, resolved_by="reviewer-1", now=T0)
        from workflow.models import STATUS_CANCELLED

        self.assertEqual(engine.state_manager.get(workflow_id).status, STATUS_CANCELLED)

    def test_ax_analyze_lifecycle_unchanged(self):
        from fastapi.testclient import TestClient
        from tests.test_mode_routing import env_for, mock_provider_runs
        from tests.test_smoke import load_app
        from workflow.models import STATUS_COMPLETED

        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        workflow_id = main_mod.router.workflow_engine.last_workflow_id
        state = main_mod.router.workflow_engine.state_manager.get(workflow_id)
        self.assertEqual(state.status, STATUS_COMPLETED)

    def test_ay_protected_without_permit_denied(self):
        from autonomy.errors import AutonomyDeniedError
        from task_queue.queue import TaskQueue
        from task_queue.worker import TaskWorker

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
        worker = TaskWorker(TaskQueue())
        with self.assertRaises(AutonomyDeniedError):
            worker.require_execution_permit(None, action=action, now=T0)

    def test_az_valid_permit_accepted(self):
        from task_queue.queue import TaskQueue
        from task_queue.worker import TaskWorker

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
        worker = TaskWorker(TaskQueue())
        worker.require_execution_permit(permit, action=action, now=T0)

    def test_ba_expired_consumed_revoked_denied(self):
        from autonomy.errors import AutonomyDeniedError
        from task_queue.queue import TaskQueue
        from task_queue.worker import TaskWorker

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
        worker = TaskWorker(TaskQueue())
        with self.assertRaises(AutonomyDeniedError):
            worker.require_execution_permit(
                permit, action=action, now=T0 + timedelta(seconds=301)
            )
        permit2 = engine._hitl().reevaluate_and_issue_permit(
            engine.last_approval_id,
            action,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            autonomy_level="executor_confirmed",
            now=T0,
        )
        engine._hitl().consume_for_execution(permit2.permit_id, action=action, now=T0)
        consumed = engine._hitl().permits.get(permit2.permit_id)
        with self.assertRaises(AutonomyDeniedError):
            worker.require_execution_permit(consumed, action=action, now=T0)

    def test_bb_queue_read_path_unchanged(self):
        from task_queue.queue import TaskQueue

        queue = TaskQueue()
        item = queue.enqueue(workflow_id="wf", task_id="t", execution_key="ek-bb")
        self.assertEqual(item.status, "queued")
        self.assertEqual(queue.dequeue().status, "leased")

    def test_bc_analyze_seven_fields(self):
        from fastapi.testclient import TestClient
        from tests.test_mode_routing import env_for, mock_provider_runs
        from tests.test_smoke import CONTRACT_KEYS, load_app

        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(len(payload), 7)

    def test_bd_fact_validator_read_only(self):
        from tools.gateway import ToolGateway
        from tools.models import TOOL_TRUST_READ_ONLY_EXTERNAL

        self.assertEqual(ToolGateway().tool_trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)

    def test_be_finops_429(self):
        from fastapi.testclient import TestClient
        from tests.test_mode_routing import env_for, mock_provider_runs
        from tests.test_smoke import load_app

        overrides = env_for("openai")
        overrides["FINOPS_UNKNOWN_COST_POLICY"] = "deny"
        main_mod = load_app(**overrides)
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(mocks["openai"].await_count, 0)

    def test_bf_mode_auto(self):
        from fastapi.testclient import TestClient
        from tests.test_mode_auto import STRATEGY_TEXT, load_auto_app
        from tests.test_mode_routing import mock_provider_runs

        main_mod = load_auto_app("anthropic", "openai", auto_order="anthropic,openai")
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "anthropic", "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "auto"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)

    def test_bg_mode_both(self):
        from fastapi.testclient import TestClient
        from tests.test_mode_routing import env_for, mock_provider_runs
        from tests.test_smoke import load_app

        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "both"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)


if __name__ == "__main__":
    unittest.main()
