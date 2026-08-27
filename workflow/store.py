from workflow.models import Checkpoint, WorkflowState


class WorkflowStateStore:
    def create(self, state: WorkflowState) -> None:
        raise NotImplementedError

    def get(self, workflow_id: str) -> WorkflowState | None:
        raise NotImplementedError

    def save(self, state: WorkflowState) -> None:
        raise NotImplementedError

    def checkpoint(self, checkpoint: Checkpoint) -> None:
        raise NotImplementedError

    def get_checkpoint(self, workflow_id: str) -> Checkpoint | None:
        raise NotImplementedError

    def list_by_status(self, status: str) -> tuple[WorkflowState, ...]:
        raise NotImplementedError

    def list_all(self) -> tuple[WorkflowState, ...]:
        raise NotImplementedError

    def find_by_execution_key(self, execution_key: str) -> WorkflowState | None:
        """Return the earliest workflow for execution_key, or None."""
        raise NotImplementedError


# P7E naming alias — same interface; no competing workflow state system.
WorkflowRuntimeStore = WorkflowStateStore


class InMemoryWorkflowStateStore(WorkflowStateStore):
    def __init__(self):
        self._states: dict[str, WorkflowState] = {}
        self._checkpoints: dict[str, Checkpoint] = {}
        self._by_execution_key: dict[str, str] = {}

    def create(self, state: WorkflowState) -> None:
        existing_id = self._by_execution_key.get(state.execution_key)
        if existing_id is not None and existing_id != state.workflow_id:
            # Keep first writer; callers should lookup before create.
            pass
        self._states[state.workflow_id] = state
        if state.execution_key and state.execution_key not in self._by_execution_key:
            self._by_execution_key[state.execution_key] = state.workflow_id

    def get(self, workflow_id: str) -> WorkflowState | None:
        return self._states.get(workflow_id)

    def save(self, state: WorkflowState) -> None:
        self._states[state.workflow_id] = state
        if state.execution_key and state.execution_key not in self._by_execution_key:
            self._by_execution_key[state.execution_key] = state.workflow_id

    def checkpoint(self, checkpoint: Checkpoint) -> None:
        self._checkpoints[checkpoint.workflow_id] = checkpoint

    def get_checkpoint(self, workflow_id: str) -> Checkpoint | None:
        return self._checkpoints.get(workflow_id)

    def list_by_status(self, status: str) -> tuple[WorkflowState, ...]:
        return tuple(
            item for item in self._states.values() if item.status == status
        )

    def list_all(self) -> tuple[WorkflowState, ...]:
        return tuple(self._states.values())

    def find_by_execution_key(self, execution_key: str) -> WorkflowState | None:
        if not execution_key:
            return None
        wf_id = self._by_execution_key.get(execution_key)
        if wf_id is not None:
            state = self._states.get(wf_id)
            if state is not None:
                return state
        # Fallback scan (handles legacy entries / key remaps)
        matches = [
            s for s in self._states.values() if s.execution_key == execution_key
        ]
        if not matches:
            return None
        matches.sort(key=lambda s: (s.created_at, s.workflow_id))
        return matches[0]
