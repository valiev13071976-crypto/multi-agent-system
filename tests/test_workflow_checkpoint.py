from dataclasses import replace
import unittest

from security.encryption import EncryptionUnavailableError
from workflow.encrypted_store import EncryptedWorkflowStateStore
from workflow.models import STATUS_RUNNING
from workflow.state_manager import StateManager
from workflow.store import InMemoryWorkflowStateStore


class WorkflowCheckpointTests(unittest.TestCase):

    def test_h_checkpoint_has_continuation_metadata(self):
        manager = StateManager()
        state = manager.create(task_id="task-check")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.start_step(state.workflow_id, "prepare_context")
        manager.complete_step(state.workflow_id, "prepare_context")
        point = manager.checkpoint(state.workflow_id)
        self.assertEqual(point.workflow_id, state.workflow_id)
        self.assertEqual(point.workflow_version, manager.get(state.workflow_id).version)
        self.assertEqual(point.status, STATUS_RUNNING)
        self.assertIn("prepare_context", point.completed_steps)
        self.assertEqual(point.payload["task_id"], "task-check")
        self.assertEqual(point.payload["next_step"], "route")
        self.assertIn("execution_key", point.payload)

    def test_i_checkpoint_does_not_store_secrets_or_prompt(self):
        manager = StateManager()
        state = manager.create(task_id="task-safe")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        point = manager.checkpoint(state.workflow_id)
        payload = dict(point.payload)
        dumped = str(payload) + str(point.completed_steps) + str(point.status)
        self.assertNotIn("Authorization", dumped)
        self.assertNotIn("PANDA_ENCRYPTION_KEY", dumped)
        self.assertNotIn("prompt", payload)
        self.assertEqual(set(payload), {"execution_key", "task_id", "next_step"})

    def test_sensitive_checkpoint_requires_encryption(self):
        store = EncryptedWorkflowStateStore(InMemoryWorkflowStateStore(), encryption=None)
        manager = StateManager(store=store)
        state = manager.create(task_id="t")
        point = manager.checkpoint(state.workflow_id)
        point = replace(point, sensitivity="sensitive")
        with self.assertRaises(EncryptionUnavailableError):
            store.checkpoint(point)


if __name__ == "__main__":
    unittest.main()
