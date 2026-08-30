from security.tenant import normalize_tenant_id
from workflow.models import Checkpoint, WorkflowState


class WorkflowStateStore:
    def create(self, state: WorkflowState) -> None:
        raise NotImplementedError

    def get(self, workflow_id: str) -> WorkflowState | None:
        raise NotImplementedError

    def get_for_tenant(
        self, workflow_id: str, tenant_id: str
    ) -> WorkflowState | None:
        """Tenant-scoped get — cross-tenant id → None."""
        raise NotImplementedError

    def save(self, state: WorkflowState) -> None:
        raise NotImplementedError

    def checkpoint(self, checkpoint: Checkpoint) -> None:
        raise NotImplementedError

    def get_checkpoint(self, workflow_id: str) -> Checkpoint | None:
        raise NotImplementedError

    def list_by_status(
        self, status: str, *, tenant_id: str | None = None
    ) -> tuple[WorkflowState, ...]:
        raise NotImplementedError

    def list_all(self) -> tuple[WorkflowState, ...]:
        """Internal/unscoped — recovery/maintenance only. Not for tenant-facing APIs."""
        raise NotImplementedError

    def find_by_execution_key(
        self, execution_key: str, *, tenant_id: str | None = None
    ) -> WorkflowState | None:
        """Return the earliest workflow for execution_key within tenant."""
        raise NotImplementedError


# P7E naming alias — same interface; no competing workflow state system.
WorkflowRuntimeStore = WorkflowStateStore


class InMemoryWorkflowStateStore(WorkflowStateStore):
    def __init__(self):
        self._states: dict[str, WorkflowState] = {}
        self._checkpoints: dict[str, Checkpoint] = {}
        self._by_execution_key: dict[str, str] = {}  # scoped_key -> workflow_id

    def _scoped_key(self, execution_key: str, tenant_id: str | None) -> str:
        return f"{normalize_tenant_id(tenant_id)}:{execution_key}"

    def create(self, state: WorkflowState) -> None:
        self._states[state.workflow_id] = state
        if state.execution_key:
            sk = self._scoped_key(state.execution_key, state.tenant_id)
            if sk not in self._by_execution_key:
                self._by_execution_key[sk] = state.workflow_id

    def get(self, workflow_id: str) -> WorkflowState | None:
        return self._states.get(workflow_id)

    def get_for_tenant(
        self, workflow_id: str, tenant_id: str
    ) -> WorkflowState | None:
        from security.tenant import workflow_tenant_id

        state = self.get(workflow_id)
        if state is None:
            return None
        if workflow_tenant_id(state) != normalize_tenant_id(tenant_id):
            return None
        return state

    def save(self, state: WorkflowState) -> None:
        self._states[state.workflow_id] = state
        if state.execution_key:
            sk = self._scoped_key(state.execution_key, state.tenant_id)
            if sk not in self._by_execution_key:
                self._by_execution_key[sk] = state.workflow_id

    def checkpoint(self, checkpoint: Checkpoint) -> None:
        self._checkpoints[checkpoint.workflow_id] = checkpoint

    def get_checkpoint(self, workflow_id: str) -> Checkpoint | None:
        return self._checkpoints.get(workflow_id)

    def list_by_status(
        self, status: str, *, tenant_id: str | None = None
    ) -> tuple[WorkflowState, ...]:
        from security.tenant import workflow_tenant_id

        items = tuple(
            item for item in self._states.values() if item.status == status
        )
        if tenant_id is None:
            return items
        tenant = normalize_tenant_id(tenant_id)
        return tuple(
            item for item in items if workflow_tenant_id(item) == tenant
        )

    def list_all(self) -> tuple[WorkflowState, ...]:
        """Internal/unscoped — recovery/maintenance only."""
        return tuple(self._states.values())

    def find_by_execution_key(
        self, execution_key: str, *, tenant_id: str | None = None
    ) -> WorkflowState | None:
        if not execution_key:
            return None
        sk = self._scoped_key(execution_key, tenant_id)
        wf_id = self._by_execution_key.get(sk)
        if wf_id is not None:
            return self._states.get(wf_id)
        tenant = normalize_tenant_id(tenant_id)
        matches = [
            s
            for s in self._states.values()
            if s.execution_key == execution_key
            and normalize_tenant_id(s.tenant_id) == tenant
        ]
        if not matches:
            return None
        matches.sort(key=lambda s: (s.created_at, s.workflow_id))
        return matches[0]
