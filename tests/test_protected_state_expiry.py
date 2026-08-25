import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from autonomy.models import utc_now
from hitl.errors import (
    ApprovalExpiredError,
    ApprovalInvalidStateError,
    ExecutionPermitExpiredError,
)
from hitl.models import PERMIT_EXPIRED, PERMIT_ISSUED, ExecutionPermit
from hitl.permit import PermitService
from side_effects.persistence import (
    attach_protected_persistence,
    build_side_effect_persistence,
)
from side_effects.protected_state_store import PersistentExecutionPermitStore
from tests.side_effect_fixtures import eval_kwargs, se_action
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from workflow.engine import WorkflowEngine
from workflow.state_manager import StateManager


class ProtectedStateExpiryTests(unittest.TestCase):
    def test_pending_approval_expires_during_downtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "exp-ap.sqlite3")
            bundle = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            engine = WorkflowEngine(
                state_manager=StateManager(store=bundle.workflow_runtime_store)
            )
            attach_protected_persistence(engine, bundle)
            stamp = utc_now()
            workflow_id = engine.create("t-exp")
            engine.state_manager.plan(workflow_id)
            engine.state_manager.start(workflow_id)
            action = se_action(
                workflow_id,
                tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
                idempotency_key="exp-ap",
            )
            kwargs = eval_kwargs(
                "executor_confirmed", capabilities=action.requested_capabilities
            )
            kwargs = dict(kwargs)
            kwargs["now"] = stamp
            engine.evaluate_action(action, requested_by="agent-1", **kwargs)
            approval_id = engine.last_approval_id
            from dataclasses import replace

            record = bundle.approval_store.get(approval_id)
            bundle.approval_store.save(
                replace(
                    record,
                    expires_at=stamp - timedelta(seconds=5),
                    version=record.version + 1,
                )
            )
            bundle.connection.close()

            bundle2 = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=True
            )
            engine2 = WorkflowEngine(
                state_manager=StateManager(store=bundle2.workflow_runtime_store)
            )
            attach_protected_persistence(engine2, bundle2)
            expired = bundle2.approval_store.get(approval_id)
            self.assertEqual(expired.status, "expired")
            with self.assertRaises((ApprovalExpiredError, ApprovalInvalidStateError)):
                engine2.hitl_service.approve(
                    approval_id, resolved_by="reviewer-1", now=utc_now()
                )
            with self.assertRaises(ApprovalInvalidStateError):
                engine2.hitl_service.reevaluate_and_issue_permit(
                    approval_id, action, **kwargs
                )
            bundle2.connection.close()

    def test_active_permit_expires_during_downtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "exp-perm.sqlite3")
            stamp = utc_now()
            bundle = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            store = PersistentExecutionPermitStore(bundle.connection)
            store.create(
                ExecutionPermit(
                    permit_id="perm-exp",
                    workflow_id="wf",
                    task_id="t",
                    action_id="a",
                    approval_id="ap",
                    decision_id="d",
                    action_fingerprint="fp",
                    issued_at=stamp - timedelta(minutes=10),
                    expires_at=stamp - timedelta(minutes=1),
                    capabilities=(CAP_EXTERNAL_WRITE,),
                    tool_id="test.write",
                    operation="set_value",
                    idempotency_key="k",
                    status=PERMIT_ISSUED,
                    version=1,
                )
            )
            bundle.connection.close()

            bundle2 = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=True
            )
            loaded = bundle2.permit_store.get("perm-exp")
            self.assertEqual(loaded.status, PERMIT_EXPIRED)
            with self.assertRaises(ExecutionPermitExpiredError):
                PermitService(store=bundle2.permit_store).validate(
                    loaded, now=utc_now()
                )
            bundle2.connection.close()


if __name__ == "__main__":
    unittest.main()
