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


# P7E naming alias — same interface; no competing workflow state system.
WorkflowRuntimeStore = WorkflowStateStore


class InMemoryWorkflowStateStore(WorkflowStateStore):
    def __init__(self):
        self._states: dict[str, WorkflowState] = {}
        self._checkpoints: dict[str, Checkpoint] = {}

    def create(self, state: WorkflowState) -> None:
        self._states[state.workflow_id] = state

    def get(self, workflow_id: str) -> WorkflowState | None:
        return self._states.get(workflow_id)

    def save(self, state: WorkflowState) -> None:
        self._states[state.workflow_id] = state

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
