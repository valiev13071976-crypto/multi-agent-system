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
    STATUS_QUEUED,
    STATUS_RETRY_WAIT,
    STATUS_RUNNING,
    STATUS_VALIDATING,
    STATUS_WAITING_APPROVAL,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
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
        step_names=None,
        workflow_type: str | None = None,
        definition_version: str | None = None,
        metadata=None,
        deadline_at=None,
        tenant_id: str | None = None,
    ) -> WorkflowState:
        now = utc_now()
        wf_id = workflow_id or str(uuid.uuid4())
        names = tuple(step_names) if step_names is not None else self._step_names
        steps = tuple(
            StepRecord(
                step_id=f"{wf_id}:{name}",
                name=name,
                status=STEP_PENDING,
                attempt=1,
            )
            for name in names
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
            tenant_id=tenant_id,
            workflow_type=workflow_type,
            definition_version=definition_version,
            metadata=dict(metadata or {}),
            next_retry_at=None,
            deadline_at=deadline_at,
        )
        self._store.create(state)
        return state

    def find_by_execution_key(self, execution_key: str, *, tenant_id: str | None = None):
        store = self._store
        if hasattr(store, "find_by_execution_key"):
            return store.find_by_execution_key(execution_key, tenant_id=tenant_id)
        from security.tenant import normalize_tenant_id

        tenant = normalize_tenant_id(tenant_id)
        for state in store.list_all():
            if state.execution_key == execution_key and normalize_tenant_id(
                state.tenant_id
            ) == tenant:
                return state
        return None

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

    def queue(self, workflow_id: str) -> WorkflowState:
        state = self.get(workflow_id)
        if state.status == STATUS_QUEUED:
            return state
        return self.transition(workflow_id, STATUS_QUEUED)

    def mark_retry_wait(
        self, workflow_id: str, *, next_retry_at, error_code: str | None = None
    ) -> WorkflowState:
        state = self.get(workflow_id)
        if state.status != STATUS_RETRY_WAIT:
            state = self.transition(workflow_id, STATUS_RETRY_WAIT)
        now = utc_now()
        state = replace(
            state,
            next_retry_at=next_retry_at,
            error_code=redact(str(error_code)) if error_code else state.error_code,
            updated_at=now,
            version=state.version + 1,
        )
        self._store.save(state)
        return state

    def clear_retry_wait(self, workflow_id: str) -> WorkflowState:
        state = self.get(workflow_id)
        if state.next_retry_at is None and state.status != STATUS_RETRY_WAIT:
            return state
        now = utc_now()
        state = replace(
            state,
            next_retry_at=None,
            updated_at=now,
            version=state.version + 1,
        )
        self._store.save(state)
        return state

    def set_metadata(self, workflow_id: str, updates: dict) -> WorkflowState:
        state = self.get(workflow_id)
        meta = dict(state.metadata)
        meta.update(updates or {})
        now = utc_now()
        state = replace(
            state,
            metadata=meta,
            updated_at=now,
            version=state.version + 1,
        )
        self._store.save(state)
        return state

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
            meta = dict(state.step(current).metadata)
            meta["approval_cleared"] = True
            state = self._update_step(
                state, current, status=STEP_PENDING, metadata=meta
            )
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
        if record.status == STEP_SKIPPED:
            return state, False
        now = utc_now()
        attempt = int(record.attempt or 1)
        if record.status in {STEP_FAILED, STEP_PENDING, STEP_WAITING}:
            # new attempt when re-entering after failure/wait
            if record.status == STEP_FAILED or (
                record.started_at is not None and record.status == STEP_PENDING
            ):
                attempt = max(attempt, 1)
        record = replace(
            record,
            status=STEP_RUNNING,
            started_at=record.started_at or now,
            attempt=attempt,
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

    def bump_step_attempt(self, workflow_id: str, name: str) -> WorkflowState:
        state = self.get(workflow_id)
        record = state.step(name)
        if record is None:
            raise WorkflowTransitionError(state.status, name)
        now = utc_now()
        record = replace(
            record,
            attempt=int(record.attempt or 1) + 1,
            status=STEP_PENDING,
            error_code=None,
            completed_at=None,
        )
        state = replace(
            state,
            steps=self._replace_step(state.steps, record),
            updated_at=now,
            version=state.version + 1,
        )
        self._store.save(state)
        return state

    def defer_step(self, workflow_id: str, name: str, *, metadata=None) -> WorkflowState:
        """Keep step pending after a bounded slice — progress in metadata, no attempt bump."""
        state = self.get(workflow_id)
        record = state.step(name)
        if record is None:
            raise WorkflowTransitionError(state.status, name)
        now = utc_now()
        meta = dict(record.metadata)
        meta.update(metadata or {})
        record = replace(
            record,
            status=STEP_PENDING,
            completed_at=None,
            error_code=None,
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
        state = self.mark_step_failed(workflow_id, name, error_code)
        return self.fail_workflow(workflow_id, error_code)

    def mark_step_failed(
        self, workflow_id: str, name: str, error_code: str
    ) -> WorkflowState:
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
        return state

    def skip_step(
        self, workflow_id: str, name: str, *, reason: str | None = None
    ) -> WorkflowState:
        state = self.get(workflow_id)
        record = state.step(name)
        if record is None:
            raise WorkflowTransitionError(state.status, name)
        if record.status in {STEP_COMPLETED, STEP_SKIPPED}:
            return state
        now = utc_now()
        meta = dict(record.metadata)
        if reason:
            meta["skip_reason"] = redact(str(reason))
        record = replace(
            record,
            status=STEP_SKIPPED,
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

    def recover_interrupted_steps(self, workflow_id: str) -> WorkflowState:
        """After process restart: running steps become pending for safe resume."""

        state = self.get(workflow_id)
        if state.status in TERMINAL_STATUSES:
            return state
        now = utc_now()
        changed = False
        steps = []
        for record in state.steps:
            if record.status == STEP_RUNNING:
                changed = True
                meta = dict(record.metadata)
                meta["recovered_from_interrupted"] = True
                steps.append(
                    replace(
                        record,
                        status=STEP_PENDING,
                        metadata=meta,
                    )
                )
            else:
                steps.append(record)
        if not changed:
            return state
        new_status = state.status
        if state.status == STATUS_RUNNING:
            new_status = STATUS_QUEUED
        state = replace(
            state,
            status=new_status,
            steps=tuple(steps),
            updated_at=now,
            version=state.version + 1,
        )
        self._store.save(state)
        return state

    def checkpoint(self, workflow_id: str, extra_payload=None) -> Checkpoint:
        state = self.get(workflow_id)
        payload = {
            "execution_key": state.execution_key,
            "task_id": state.task_id,
            "next_step": state.next_incomplete_step(),
        }
        if extra_payload:
            from workflow.checkpoint import approval_checkpoint_fields

            payload.update(approval_checkpoint_fields(extra_payload))
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
