from dataclasses import FrozenInstanceError
import unittest

from workflow.errors import WorkflowTransitionError
from workflow.models import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_RUNNING,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
)
from workflow.state_manager import StateManager


class WorkflowStateTests(unittest.TestCase):

    def test_a_new_workflow_is_created(self):
        manager = StateManager()
        state = manager.create(task_id="task-1")
        self.assertEqual(state.status, STATUS_CREATED)
        self.assertEqual(state.task_id, "task-1")
        self.assertEqual(state.version, 1)
        self.assertIsNotNone(state.workflow_id)
        self.assertNotEqual(state.workflow_id, state.task_id)

    def test_state_is_immutable(self):
        state = StateManager().create(task_id="t")
        with self.assertRaises(FrozenInstanceError):
            state.status = STATUS_RUNNING

    def test_b_valid_transition_increments_version(self):
        manager = StateManager()
        state = manager.create(task_id="t")
        planned = manager.plan(state.workflow_id)
        self.assertEqual(planned.status, STATUS_PLANNED)
        self.assertEqual(planned.version, state.version + 1)
        running = manager.start(state.workflow_id)
        self.assertEqual(running.status, STATUS_RUNNING)
        self.assertGreater(running.version, planned.version)

    def test_c_invalid_transition_raises(self):
        manager = StateManager()
        state = manager.create(task_id="t")
        with self.assertRaises(WorkflowTransitionError):
            manager.transition(state.workflow_id, STATUS_COMPLETED)

    def test_d_terminal_completed_cannot_run_again(self):
        manager = StateManager()
        state = manager.create(task_id="t")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.complete_workflow(state.workflow_id)
        with self.assertRaises(WorkflowTransitionError):
            manager.start(state.workflow_id)
        self.assertEqual(manager.get(state.workflow_id).status, STATUS_COMPLETED)

    def test_e_step_pending_running_completed(self):
        manager = StateManager()
        state = manager.create(task_id="t")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        record = state.step("prepare_context")
        self.assertEqual(record.status, STEP_PENDING)
        manager.start_step(state.workflow_id, "prepare_context")
        self.assertEqual(
            manager.get(state.workflow_id).step("prepare_context").status,
            STEP_RUNNING,
        )
        manager.complete_step(state.workflow_id, "prepare_context")
        self.assertEqual(
            manager.get(state.workflow_id).step("prepare_context").status,
            STEP_COMPLETED,
        )

    def test_f_failed_step_fails_workflow(self):
        manager = StateManager()
        state = manager.create(task_id="t")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.start_step(state.workflow_id, "route")
        manager.fail_step(state.workflow_id, "route", "invalid_mode")
        failed = manager.get(state.workflow_id)
        self.assertEqual(failed.status, STATUS_FAILED)
        self.assertEqual(failed.step("route").status, STEP_FAILED)
        self.assertEqual(failed.error_code, "invalid_mode")

    def test_n_transitions_are_deterministic(self):
        first = StateManager()
        second = StateManager()
        a = first.create(task_id="t")
        b = second.create(task_id="t")
        first.plan(a.workflow_id)
        first.start(a.workflow_id)
        second.plan(b.workflow_id)
        second.start(b.workflow_id)
        self.assertEqual(first.get(a.workflow_id).status, second.get(b.workflow_id).status)
        self.assertEqual(first.get(a.workflow_id).version, second.get(b.workflow_id).version)

    def test_cancel_is_terminal(self):
        manager = StateManager()
        state = manager.create(task_id="t")
        manager.cancel(state.workflow_id)
        self.assertEqual(manager.get(state.workflow_id).status, STATUS_CANCELLED)
        with self.assertRaises(WorkflowTransitionError):
            manager.plan(state.workflow_id)


if __name__ == "__main__":
    unittest.main()
