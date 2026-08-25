from dataclasses import replace
import uuid

from security.redaction import redact
from workflow.errors import (
    WorkflowNotFoundError,
    WorkflowTransitionError,
)
from workflow.models import (
    ALLOWED_TRANSITIONS,
    ANALYZE_STEPS,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_RUNNING,
    STATUS_VALIDATING,
    STATUS_WAITING_APPROVAL,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_WAITING,
    TERMINAL_STATUSES,
    Checkpoint,
    StepRecord,
    WorkflowState,
    utc_now,
)
from workflow.store import InMemoryWorkflowStateStore, WorkflowStateStore


class StateManager:
    def __init__(self, store: WorkflowStateStore | None = None, step_names=ANALYZE_STEPS):
        self._store = store or InMemoryWorkflowStateStore()
        self._step_names = tuple(step_names)

    def create(
        self,
        *,
        task_id: str,
        workflow_id: str | None = None,
        execution_key: str | None = None,
    ) -> WorkflowState:
        now = utc_now()
        wf_id = workflow_id or str(uuid.uuid4())
        steps = tuple(
            StepRecord(
                step_id=f"{wf_id}:{name}",
                name=name,
                status=STEP_PENDING,
                attempt=1,
            )
            for name in self._step_names
        )
        state = WorkflowState(
            workflow_id=wf_id,
            task_id=task_id,
            status=STATUS_CREATED,
            current_step=None,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
            failed_at=None,
            error_code=None,
            version=1,
            steps=steps,
            execution_key=execution_key or str(uuid.uuid4()),
        )
        self._store.create(state)
        return state

    def get(self, workflow_id: str) -> WorkflowState:
        state = self._store.get(workflow_id)
        if state is None:
            raise WorkflowNotFoundError(workflow_id)
        return state

    def transition(self, workflow_id: str, target: str) -> WorkflowState:
        state = self.get(workflow_id)
        allowed = ALLOWED_TRANSITIONS.get(state.status, frozenset())
        if target not in allowed:
            raise WorkflowTransitionError(state.status, target)
        now = utc_now()
        updates = {
            "status": target,
            "updated_at": now,
            "version": state.version + 1,
        }
        if target == STATUS_RUNNING and state.started_at is None:
            updates["started_at"] = now
        if target == STATUS_COMPLETED:
            updates["completed_at"] = now
        if target == STATUS_FAILED:
            updates["failed_at"] = now
        state = replace(state, **updates)
        self._store.save(state)
        return state

    def plan(self, workflow_id: str) -> WorkflowState:
        return self.transition(workflow_id, STATUS_PLANNED)

    def start(self, workflow_id: str) -> WorkflowState:
        return self.transition(workflow_id, STATUS_RUNNING)

    def mark_validating(self, workflow_id: str) -> WorkflowState:
        state = self.get(workflow_id)
        if state.status == STATUS_VALIDATING:
            return state
        return self.transition(workflow_id, STATUS_VALIDATING)

    def mark_running(self, workflow_id: str) -> WorkflowState:
        state = self.get(workflow_id)
        if state.status == STATUS_RUNNING:
            return state
        return self.transition(workflow_id, STATUS_RUNNING)

    def wait_for_approval(self, workflow_id: str) -> WorkflowState:
        state = self.transition(workflow_id, STATUS_WAITING_APPROVAL)
        current = state.current_step
        if current:
            state = self._update_step(
                state,
                current,
                status=STEP_WAITING,
            )
            self._store.save(state)
        return state

    def approve(self, workflow_id: str) -> WorkflowState:
        state = self.transition(workflow_id, STATUS_RUNNING)
        current = state.current_step
        if current and state.step(current) and state.step(current).status == STEP_WAITING:
            state = self._update_step(state, current, status=STEP_PENDING)
            self._store.save(state)
        return state

    def complete_workflow(self, workflow_id: str) -> WorkflowState:
        state = self.get(workflow_id)
        if state.status == STATUS_COMPLETED:
            return state
        if state.status == STATUS_VALIDATING:
            state = self.transition(workflow_id, STATUS_RUNNING)
        return self.transition(state.workflow_id, STATUS_COMPLETED)

    def fail_workflow(self, workflow_id: str, error_code: str) -> WorkflowState:
        state = self.get(workflow_id)
        code = redact(str(error_code or "workflow_failed"))
        if state.status == STATUS_FAILED:
            if state.error_code:
                return state
            state = replace(state, error_code=code, version=state.version + 1, updated_at=utc_now())
            self._store.save(state)
            return state
        state = self.transition(workflow_id, STATUS_FAILED)
        state = replace(state, error_code=code, version=state.version + 1, updated_at=utc_now())
        self._store.save(state)
        return state

    def cancel(self, workflow_id: str) -> WorkflowState:
        return self.transition(workflow_id, STATUS_CANCELLED)

    def start_step(self, workflow_id: str, name: str) -> tuple[WorkflowState, bool]:
        state = self.get(workflow_id)
        record = state.step(name)
        if record is None:
            raise WorkflowTransitionError(state.status, name)
        if record.status == STEP_COMPLETED:
            return state, False
        now = utc_now()
        record = replace(
            record,
            status=STEP_RUNNING,
            started_at=record.started_at or now,
            attempt=record.attempt or 1,
            error_code=None,
        )
        state = replace(
            state,
            current_step=name,
            steps=self._replace_step(state.steps, record),
            updated_at=now,
            version=state.version + 1,
        )
        self._store.save(state)
        return state, True

    def complete_step(self, workflow_id: str, name: str, metadata=None) -> WorkflowState:
        state = self.get(workflow_id)
        record = state.step(name)
        if record is None:
            raise WorkflowTransitionError(state.status, name)
        if record.status == STEP_COMPLETED:
            return state
        now = utc_now()
        meta = dict(record.metadata)
        meta.update(metadata or {})
        record = replace(
            record,
            status=STEP_COMPLETED,
            completed_at=now,
            metadata=meta,
        )
        state = replace(
            state,
            steps=self._replace_step(state.steps, record),
            updated_at=now,
            version=state.version + 1,
        )
        self._store.save(state)
        return state

    def fail_step(self, workflow_id: str, name: str, error_code: str) -> WorkflowState:
        state = self.get(workflow_id)
        record = state.step(name)
        now = utc_now()
        code = redact(str(error_code or "step_failed"))
        if record is not None:
            record = replace(
                record,
                status=STEP_FAILED,
                completed_at=now,
                error_code=code,
            )
            state = replace(
                state,
                current_step=name,
                steps=self._replace_step(state.steps, record),
                updated_at=now,
                version=state.version + 1,
            )
            self._store.save(state)
        return self.fail_workflow(workflow_id, code)

    def checkpoint(self, workflow_id: str) -> Checkpoint:
        state = self.get(workflow_id)
        payload = {
            "execution_key": state.execution_key,
            "task_id": state.task_id,
            "next_step": state.next_incomplete_step(),
        }
        point = Checkpoint(
            workflow_id=state.workflow_id,
            workflow_version=state.version,
            status=state.status,
            current_step=state.current_step,
            completed_steps=state.completed_step_names(),
            timestamp=utc_now(),
            payload=payload,
            sensitivity="internal",
        )
        self._store.checkpoint(point)
        return point

    def get_checkpoint(self, workflow_id: str) -> Checkpoint | None:
        return self._store.get_checkpoint(workflow_id)

    def _replace_step(self, steps: tuple[StepRecord, ...], record: StepRecord):
        return tuple(record if item.name == record.name else item for item in steps)

    def _update_step(self, state: WorkflowState, name: str, **changes) -> WorkflowState:
        record = state.step(name)
        if record is None:
            return state
        record = replace(record, **changes)
        return replace(
            state,
            steps=self._replace_step(state.steps, record),
            updated_at=utc_now(),
            version=state.version + 1,
        )
