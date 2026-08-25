"""Durable HITL approval, execution-permit, and workflow-runtime SQLite stores (P7E)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any

from autonomy.models import (
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    ApprovalRecord,
    sanitize_metadata,
    utc_now,
)
from autonomy.store import ApprovalStore
from hitl.errors import ApprovalConflictError, ExecutionPermitConflictError
from hitl.models import (
    PERMIT_EXPIRED,
    PERMIT_ISSUED,
    ExecutionPermit,
)
from hitl.store import ExecutionPermitStore
from security.encryption import (
    ENCRYPTION_REQUIRED,
    SENSITIVITY_INTERNAL,
    SENSITIVITY_SENSITIVE,
    EncryptionService,
)
from side_effects.errors import (
    SideEffectPersistenceUnavailableError,
)
from side_effects.sqlite_store import (
    SqliteConnection,
    _dt_from_db,
    _dt_to_db,
    _merge_payload,
    _split_payload,
)
from workflow.errors import WorkflowConflictError
from workflow.models import (
    STATUS_WAITING_APPROVAL,
    Checkpoint,
    StepRecord,
    WorkflowState,
)
from workflow.store import WorkflowStateStore


APPROVAL_SENSITIVE_KEYS = frozenset(
    {
        "resource_ref",
        "resource",
        "decision_reason",
        "reason",
        "requester_metadata",
        "approver_metadata",
    }
)
PERMIT_SENSITIVE_KEYS = frozenset({"resource_ref", "resource"})
WORKFLOW_SENSITIVE_KEYS = frozenset({"prompt", "payload_secret"})


def _capabilities_dumps(caps: tuple[str, ...]) -> str:
    return json.dumps(list(caps), separators=(",", ":"), sort_keys=False)


def _capabilities_loads(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    data = json.loads(raw)
    if not isinstance(data, list):
        return ()
    return tuple(str(item) for item in data)


def _steps_to_json(steps: tuple[StepRecord, ...]) -> str:
    payload = []
    for step in steps:
        payload.append(
            {
                "step_id": step.step_id,
                "name": step.name,
                "status": step.status,
                "started_at": _dt_to_db(step.started_at),
                "completed_at": _dt_to_db(step.completed_at),
                "attempt": int(step.attempt),
                "error_code": step.error_code,
                "metadata": dict(step.metadata),
            }
        )
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _steps_from_json(raw: str | None) -> tuple[StepRecord, ...]:
    if not raw:
        return ()
    data = json.loads(raw)
    if not isinstance(data, list):
        return ()
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            StepRecord(
                step_id=str(item.get("step_id") or ""),
                name=str(item.get("name") or ""),
                status=str(item.get("status") or "pending"),
                started_at=_dt_from_db(item.get("started_at")),
                completed_at=_dt_from_db(item.get("completed_at")),
                attempt=int(item.get("attempt") or 1),
                error_code=item.get("error_code"),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return tuple(out)


class PersistentApprovalStore(ApprovalStore):
    """Durable ApprovalStore. Business logic stays in HITLService."""

    def __init__(
        self,
        connection: SqliteConnection,
        *,
        encryption: EncryptionService | None = None,
        auto_expire: bool = False,
    ):
        self._connection = connection
        self._encryption = encryption
        self._auto_expire = auto_expire

    def put(self, record: ApprovalRecord) -> None:
        existing = self.get(record.approval_id, normalize=False)
        if existing is None:
            self.create(record)
        else:
            self.save(record)

    def create(self, record: ApprovalRecord) -> None:
        self._upsert(record, insert=True)

    def save(self, record: ApprovalRecord) -> None:
        self._upsert(record, insert=False)

    def get(
        self, approval_id: str, *, normalize: bool | None = None
    ) -> ApprovalRecord | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM hitl_approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is None:
            return None
        record = self._from_row(row)
        if normalize is None:
            normalize = self._auto_expire
        if normalize:
            return self._maybe_expire(record)
        return record

    def list_for_action(self, action_id: str) -> tuple[ApprovalRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM hitl_approvals WHERE action_id = ?",
            (action_id,),
        ).fetchall()
        return tuple(self._maybe_expire(self._from_row(row)) for row in rows)

    def find_pending_by_action(self, action_id: str) -> ApprovalRecord | None:
        for item in self.list_for_action(action_id):
            if item.status == APPROVAL_PENDING:
                return item
        return None

    def list_pending(self) -> tuple[ApprovalRecord, ...]:
        return self.list_by_status(APPROVAL_PENDING)

    def list_by_workflow(self, workflow_id: str) -> tuple[ApprovalRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM hitl_approvals WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        return tuple(self._maybe_expire(self._from_row(row)) for row in rows)

    def list_by_status(self, status: str) -> tuple[ApprovalRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM hitl_approvals WHERE status = ?",
            (status,),
        ).fetchall()
        return tuple(self._maybe_expire(self._from_row(row)) for row in rows)

    def list_all(self) -> tuple[ApprovalRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute("SELECT * FROM hitl_approvals").fetchall()
        return tuple(self._maybe_expire(self._from_row(row)) for row in rows)

    def normalize_expired(self, *, now: datetime | None = None) -> int:
        stamp = now or utc_now()
        changed = 0
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM hitl_approvals WHERE status = ?",
            (APPROVAL_PENDING,),
        ).fetchall()
        for row in rows:
            record = self._from_row(row)
            if record.expires_at is not None and record.expires_at <= stamp:
                self._force_expire(record, stamp)
                changed += 1
        return changed

    def _maybe_expire(self, record: ApprovalRecord) -> ApprovalRecord:
        if not self._auto_expire:
            return record
        if record.status != APPROVAL_PENDING:
            return record
        stamp = utc_now()
        if record.expires_at is not None and record.expires_at <= stamp:
            return self._force_expire(record, stamp)
        return record

    def _force_expire(self, record: ApprovalRecord, stamp: datetime) -> ApprovalRecord:
        updated = replace(
            record,
            status=APPROVAL_EXPIRED,
            approved_by="system",
            resolved_by="system",
            resolved_at=stamp,
            reason_code="approval_expired",
            version=int(record.version) + 1,
        )
        try:
            self.save(updated)
        except ApprovalConflictError:
            current = self.get(record.approval_id, normalize=False)
            return current or updated
        return updated

    def _upsert(self, record: ApprovalRecord, *, insert: bool) -> None:
        sensitivity = SENSITIVITY_INTERNAL
        meta = dict(record.metadata)
        if any(key in meta for key in APPROVAL_SENSITIVE_KEYS):
            sensitivity = SENSITIVITY_SENSITIVE
        safe_json, encrypted_json = _split_payload(
            meta,
            sensitivity=sensitivity,
            encryption=self._encryption,
            sensitive_keys=APPROVAL_SENSITIVE_KEYS,
        )
        conn = self._connection.connect()
        try:
            if insert:
                conn.execute(
                    """
                    INSERT INTO hitl_approvals (
                        approval_id, workflow_id, task_id, action_id, decision_id,
                        status, approved_by, created_at, resolved_at, reason_code,
                        approval_class, requested_by, resolved_by, requested_at,
                        expires_at, version, action_fingerprint, required_approvals,
                        sensitivity, safe_metadata_json, encrypted_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.approval_id,
                        record.workflow_id,
                        record.task_id,
                        record.action_id,
                        record.decision_id,
                        record.status,
                        record.approved_by,
                        _dt_to_db(record.created_at),
                        _dt_to_db(record.resolved_at),
                        record.reason_code,
                        record.approval_class,
                        record.requested_by or "",
                        record.resolved_by,
                        _dt_to_db(record.requested_at),
                        _dt_to_db(record.expires_at),
                        int(record.version),
                        record.action_fingerprint or "",
                        int(record.required_approvals),
                        sensitivity,
                        safe_json,
                        encrypted_json,
                    ),
                )
            else:
                expected = max(int(record.version) - 1, 0)
                cur = conn.execute(
                    """
                    UPDATE hitl_approvals SET
                        workflow_id=?, task_id=?, action_id=?, decision_id=?,
                        status=?, approved_by=?, created_at=?, resolved_at=?,
                        reason_code=?, approval_class=?, requested_by=?,
                        resolved_by=?, requested_at=?, expires_at=?, version=?,
                        action_fingerprint=?, required_approvals=?, sensitivity=?,
                        safe_metadata_json=?, encrypted_payload_json=?
                    WHERE approval_id=? AND version=?
                    """,
                    (
                        record.workflow_id,
                        record.task_id,
                        record.action_id,
                        record.decision_id,
                        record.status,
                        record.approved_by,
                        _dt_to_db(record.created_at),
                        _dt_to_db(record.resolved_at),
                        record.reason_code,
                        record.approval_class,
                        record.requested_by or "",
                        record.resolved_by,
                        _dt_to_db(record.requested_at),
                        _dt_to_db(record.expires_at),
                        int(record.version),
                        record.action_fingerprint or "",
                        int(record.required_approvals),
                        sensitivity,
                        safe_json,
                        encrypted_json,
                        record.approval_id,
                        expected,
                    ),
                )
                if cur.rowcount != 1:
                    raise ApprovalConflictError()
            self._connection.maybe_autocommit()
        except ApprovalConflictError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ApprovalConflictError("duplicate_active_approval") from exc
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "protected_state_persistence_unavailable"
            ) from exc

    def _from_row(self, row: sqlite3.Row) -> ApprovalRecord:
        metadata = _merge_payload(
            row["safe_metadata_json"],
            row["encrypted_payload_json"],
            encryption=self._encryption,
        )
        return ApprovalRecord(
            approval_id=row["approval_id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            action_id=row["action_id"],
            decision_id=row["decision_id"],
            status=row["status"],
            approved_by=row["approved_by"],
            created_at=_dt_from_db(row["created_at"]),
            resolved_at=_dt_from_db(row["resolved_at"]),
            reason_code=row["reason_code"],
            approval_class=row["approval_class"] or "standard",
            requested_by=row["requested_by"] or "",
            resolved_by=row["resolved_by"],
            requested_at=_dt_from_db(row["requested_at"]),
            expires_at=_dt_from_db(row["expires_at"]),
            version=int(row["version"]),
            action_fingerprint=row["action_fingerprint"] or "",
            required_approvals=int(row["required_approvals"] or 1),
            metadata=metadata,
        )


class PersistentExecutionPermitStore(ExecutionPermitStore):
    """Durable ExecutionPermitStore. No bearer/signature material stored."""

    def __init__(
        self,
        connection: SqliteConnection,
        *,
        encryption: EncryptionService | None = None,
        auto_expire: bool = False,
    ):
        self._connection = connection
        self._encryption = encryption
        self._auto_expire = auto_expire

    def create(self, permit: ExecutionPermit) -> None:
        self._upsert(permit, insert=True)

    def get(
        self, permit_id: str, *, normalize: bool | None = None
    ) -> ExecutionPermit | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM execution_permits WHERE permit_id = ?",
            (permit_id,),
        ).fetchone()
        if row is None:
            return None
        permit = self._from_row(row)
        if normalize is None:
            normalize = self._auto_expire
        if normalize:
            return self._maybe_expire(permit)
        return permit

    def save(self, permit: ExecutionPermit) -> None:
        self._upsert(permit, insert=False)

    def find_active_by_approval(self, approval_id: str) -> ExecutionPermit | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM execution_permits WHERE approval_id = ? AND status = ?",
            (approval_id, PERMIT_ISSUED),
        ).fetchone()
        if row is None:
            return None
        return self._maybe_expire(self._from_row(row))

    def list_by_status(self, status: str) -> tuple[ExecutionPermit, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM execution_permits WHERE status = ?",
            (status,),
        ).fetchall()
        return tuple(self._maybe_expire(self._from_row(row)) for row in rows)

    def list_all(self) -> tuple[ExecutionPermit, ...]:
        conn = self._connection.connect()
        rows = conn.execute("SELECT * FROM execution_permits").fetchall()
        return tuple(self._maybe_expire(self._from_row(row)) for row in rows)

    def normalize_expired(self, *, now: datetime | None = None) -> int:
        stamp = now or utc_now()
        changed = 0
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM execution_permits WHERE status = ?",
            (PERMIT_ISSUED,),
        ).fetchall()
        for row in rows:
            permit = self._from_row(row)
            if permit.expires_at <= stamp:
                self._force_expire(permit)
                changed += 1
        return changed

    def _maybe_expire(self, permit: ExecutionPermit) -> ExecutionPermit:
        if not self._auto_expire:
            return permit
        if permit.status != PERMIT_ISSUED:
            return permit
        if permit.expires_at <= utc_now():
            return self._force_expire(permit)
        return permit

    def _force_expire(self, permit: ExecutionPermit) -> ExecutionPermit:
        updated = replace(
            permit,
            status=PERMIT_EXPIRED,
            version=int(permit.version) + 1,
        )
        try:
            self.save(updated)
        except ExecutionPermitConflictError:
            current = self.get(permit.permit_id, normalize=False)
            return current or updated
        return updated

    def _upsert(self, permit: ExecutionPermit, *, insert: bool) -> None:
        meta = sanitize_metadata(dict(permit.metadata))
        for forbidden in ("signature", "token", "bearer", "raw_permit", "permit_token"):
            meta.pop(forbidden, None)
        sensitivity = SENSITIVITY_INTERNAL
        if any(key in meta for key in PERMIT_SENSITIVE_KEYS):
            sensitivity = SENSITIVITY_SENSITIVE
        safe_json, encrypted_json = _split_payload(
            meta,
            sensitivity=sensitivity,
            encryption=self._encryption,
            sensitive_keys=PERMIT_SENSITIVE_KEYS,
        )
        conn = self._connection.connect()
        try:
            if insert:
                conn.execute(
                    """
                    INSERT INTO execution_permits (
                        permit_id, workflow_id, task_id, action_id, approval_id,
                        decision_id, action_fingerprint, issued_at, expires_at,
                        consumed_at, status, version, capabilities_json, tool_id,
                        operation, idempotency_key, single_use, sensitivity,
                        safe_metadata_json, encrypted_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        permit.permit_id,
                        permit.workflow_id,
                        permit.task_id,
                        permit.action_id,
                        permit.approval_id,
                        permit.decision_id,
                        permit.action_fingerprint,
                        _dt_to_db(permit.issued_at),
                        _dt_to_db(permit.expires_at),
                        _dt_to_db(permit.consumed_at),
                        permit.status,
                        int(permit.version),
                        _capabilities_dumps(permit.capabilities),
                        permit.tool_id,
                        permit.operation,
                        permit.idempotency_key,
                        1 if permit.single_use else 0,
                        sensitivity,
                        safe_json,
                        encrypted_json,
                    ),
                )
            else:
                expected = max(int(permit.version) - 1, 0)
                cur = conn.execute(
                    """
                    UPDATE execution_permits SET
                        workflow_id=?, task_id=?, action_id=?, approval_id=?,
                        decision_id=?, action_fingerprint=?, issued_at=?,
                        expires_at=?, consumed_at=?, status=?, version=?,
                        capabilities_json=?, tool_id=?, operation=?,
                        idempotency_key=?, single_use=?, sensitivity=?,
                        safe_metadata_json=?, encrypted_payload_json=?
                    WHERE permit_id=? AND version=?
                    """,
                    (
                        permit.workflow_id,
                        permit.task_id,
                        permit.action_id,
                        permit.approval_id,
                        permit.decision_id,
                        permit.action_fingerprint,
                        _dt_to_db(permit.issued_at),
                        _dt_to_db(permit.expires_at),
                        _dt_to_db(permit.consumed_at),
                        permit.status,
                        int(permit.version),
                        _capabilities_dumps(permit.capabilities),
                        permit.tool_id,
                        permit.operation,
                        permit.idempotency_key,
                        1 if permit.single_use else 0,
                        sensitivity,
                        safe_json,
                        encrypted_json,
                        permit.permit_id,
                        expected,
                    ),
                )
                if cur.rowcount != 1:
                    raise ExecutionPermitConflictError()
            self._connection.maybe_autocommit()
        except ExecutionPermitConflictError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ExecutionPermitConflictError("duplicate_active_permit") from exc
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "protected_state_persistence_unavailable"
            ) from exc

    def _from_row(self, row: sqlite3.Row) -> ExecutionPermit:
        metadata = _merge_payload(
            row["safe_metadata_json"],
            row["encrypted_payload_json"],
            encryption=self._encryption,
        )
        return ExecutionPermit(
            permit_id=row["permit_id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            action_id=row["action_id"],
            approval_id=row["approval_id"],
            decision_id=row["decision_id"],
            action_fingerprint=row["action_fingerprint"],
            issued_at=_dt_from_db(row["issued_at"]),
            expires_at=_dt_from_db(row["expires_at"]),
            capabilities=_capabilities_loads(row["capabilities_json"]),
            tool_id=row["tool_id"],
            operation=row["operation"],
            idempotency_key=row["idempotency_key"],
            single_use=bool(row["single_use"]),
            status=row["status"],
            consumed_at=_dt_from_db(row["consumed_at"]),
            version=int(row["version"]),
            metadata=metadata,
        )


class PersistentWorkflowRuntimeStore(WorkflowStateStore):
    """Durable WorkflowRuntimeStore. Does not reopen terminal workflows."""

    def __init__(
        self,
        connection: SqliteConnection,
        *,
        encryption: EncryptionService | None = None,
    ):
        self._connection = connection
        self._encryption = encryption

    def create(self, state: WorkflowState) -> None:
        self._upsert(state, insert=True, linkage=None)

    def get(self, workflow_id: str) -> WorkflowState | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM workflow_runtime_state WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def save(self, state: WorkflowState, *, linkage: dict | None = None) -> None:
        self._upsert(state, insert=False, linkage=linkage)

    def checkpoint(self, checkpoint: Checkpoint) -> None:
        sensitivity = checkpoint.sensitivity or SENSITIVITY_INTERNAL
        payload = dict(checkpoint.payload)
        if sensitivity in ENCRYPTION_REQUIRED and self._encryption is None:
            raise SideEffectPersistenceUnavailableError(
                "protected_state_persistence_unavailable"
            )
        safe_json, encrypted_json = _split_payload(
            payload,
            sensitivity=sensitivity,
            encryption=self._encryption,
            sensitive_keys=WORKFLOW_SENSITIVE_KEYS,
        )
        completed = json.dumps(
            list(checkpoint.completed_steps), separators=(",", ":"), sort_keys=True
        )
        conn = self._connection.connect()
        try:
            conn.execute(
                """
                INSERT INTO workflow_checkpoints (
                    workflow_id, workflow_version, status, current_step,
                    completed_steps_json, timestamp, sensitivity,
                    safe_metadata_json, encrypted_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    workflow_version=excluded.workflow_version,
                    status=excluded.status,
                    current_step=excluded.current_step,
                    completed_steps_json=excluded.completed_steps_json,
                    timestamp=excluded.timestamp,
                    sensitivity=excluded.sensitivity,
                    safe_metadata_json=excluded.safe_metadata_json,
                    encrypted_payload_json=excluded.encrypted_payload_json
                """,
                (
                    checkpoint.workflow_id,
                    int(checkpoint.workflow_version),
                    checkpoint.status,
                    checkpoint.current_step,
                    completed,
                    _dt_to_db(checkpoint.timestamp),
                    sensitivity,
                    safe_json,
                    encrypted_json,
                ),
            )
            # Keep runtime linkage columns in sync with checkpoint approval fields.
            approval_id = payload.get("approval_id")
            action_id = payload.get("action_id")
            if approval_id is not None or action_id is not None:
                conn.execute(
                    """
                    UPDATE workflow_runtime_state SET
                        approval_id=COALESCE(?, approval_id),
                        action_id=COALESCE(?, action_id),
                        waiting_reason=CASE
                            WHEN state = ? THEN COALESCE(waiting_reason, 'waiting_approval')
                            ELSE waiting_reason
                        END
                    WHERE workflow_id=?
                    """,
                    (
                        None if approval_id is None else str(approval_id),
                        None if action_id is None else str(action_id),
                        STATUS_WAITING_APPROVAL,
                        checkpoint.workflow_id,
                    ),
                )
            self._connection.maybe_autocommit()
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "protected_state_persistence_unavailable"
            ) from exc

    def get_checkpoint(self, workflow_id: str) -> Checkpoint | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM workflow_checkpoints WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _merge_payload(
            row["safe_metadata_json"],
            row["encrypted_payload_json"],
            encryption=self._encryption,
        )
        completed_raw = row["completed_steps_json"] or "[]"
        completed = json.loads(completed_raw)
        if not isinstance(completed, list):
            completed = []
        return Checkpoint(
            workflow_id=row["workflow_id"],
            workflow_version=int(row["workflow_version"]),
            status=row["status"],
            current_step=row["current_step"],
            completed_steps=tuple(str(item) for item in completed),
            timestamp=_dt_from_db(row["timestamp"]),
            payload=payload,
            sensitivity=row["sensitivity"] or SENSITIVITY_INTERNAL,
        )

    def list_by_status(self, status: str) -> tuple[WorkflowState, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM workflow_runtime_state WHERE state = ?",
            (status,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_all(self) -> tuple[WorkflowState, ...]:
        conn = self._connection.connect()
        rows = conn.execute("SELECT * FROM workflow_runtime_state").fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_waiting_approval(self) -> tuple[WorkflowState, ...]:
        return self.list_by_status(STATUS_WAITING_APPROVAL)

    def _upsert(
        self,
        state: WorkflowState,
        *,
        insert: bool,
        linkage: dict | None,
    ) -> None:
        meta: dict[str, Any] = {
            "steps_json": _steps_to_json(state.steps),
        }
        waiting_reason = None
        approval_id = None
        action_id = None
        if state.status == STATUS_WAITING_APPROVAL:
            waiting_reason = "waiting_approval"
        if linkage:
            approval_id = linkage.get("approval_id")
            action_id = linkage.get("action_id")
            waiting_reason = linkage.get("waiting_reason", waiting_reason)
        safe_json, encrypted_json = _split_payload(
            meta,
            sensitivity=SENSITIVITY_INTERNAL,
            encryption=self._encryption,
            sensitive_keys=WORKFLOW_SENSITIVE_KEYS,
        )
        conn = self._connection.connect()
        try:
            if insert:
                conn.execute(
                    """
                    INSERT INTO workflow_runtime_state (
                        workflow_id, task_id, state, current_step, waiting_reason,
                        approval_id, action_id, created_at, updated_at, started_at,
                        completed_at, failed_at, error_code, execution_key, version,
                        sensitivity, safe_metadata_json, encrypted_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.workflow_id,
                        state.task_id,
                        state.status,
                        state.current_step,
                        waiting_reason,
                        None if approval_id is None else str(approval_id),
                        None if action_id is None else str(action_id),
                        _dt_to_db(state.created_at),
                        _dt_to_db(state.updated_at),
                        _dt_to_db(state.started_at),
                        _dt_to_db(state.completed_at),
                        _dt_to_db(state.failed_at),
                        state.error_code,
                        state.execution_key,
                        int(state.version),
                        SENSITIVITY_INTERNAL,
                        safe_json,
                        encrypted_json,
                    ),
                )
            else:
                expected = max(int(state.version) - 1, 0)
                existing = conn.execute(
                    "SELECT approval_id, action_id, waiting_reason FROM "
                    "workflow_runtime_state WHERE workflow_id = ?",
                    (state.workflow_id,),
                ).fetchone()
                if existing is not None:
                    if approval_id is None:
                        approval_id = existing["approval_id"]
                    if action_id is None:
                        action_id = existing["action_id"]
                    if waiting_reason is None and state.status == STATUS_WAITING_APPROVAL:
                        waiting_reason = existing["waiting_reason"] or "waiting_approval"
                    if state.status != STATUS_WAITING_APPROVAL:
                        waiting_reason = None
                cur = conn.execute(
                    """
                    UPDATE workflow_runtime_state SET
                        task_id=?, state=?, current_step=?, waiting_reason=?,
                        approval_id=?, action_id=?, created_at=?, updated_at=?,
                        started_at=?, completed_at=?, failed_at=?, error_code=?,
                        execution_key=?, version=?, sensitivity=?,
                        safe_metadata_json=?, encrypted_payload_json=?
                    WHERE workflow_id=? AND version=?
                    """,
                    (
                        state.task_id,
                        state.status,
                        state.current_step,
                        waiting_reason,
                        None if approval_id is None else str(approval_id),
                        None if action_id is None else str(action_id),
                        _dt_to_db(state.created_at),
                        _dt_to_db(state.updated_at),
                        _dt_to_db(state.started_at),
                        _dt_to_db(state.completed_at),
                        _dt_to_db(state.failed_at),
                        state.error_code,
                        state.execution_key,
                        int(state.version),
                        SENSITIVITY_INTERNAL,
                        safe_json,
                        encrypted_json,
                        state.workflow_id,
                        expected,
                    ),
                )
                if cur.rowcount != 1:
                    raise WorkflowConflictError("workflow_version_conflict")
            self._connection.maybe_autocommit()
        except WorkflowConflictError:
            raise
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "protected_state_persistence_unavailable"
            ) from exc

    def _from_row(self, row: sqlite3.Row) -> WorkflowState:
        metadata = _merge_payload(
            row["safe_metadata_json"],
            row["encrypted_payload_json"],
            encryption=self._encryption,
        )
        steps_raw = metadata.get("steps_json")
        if isinstance(steps_raw, str):
            steps = _steps_from_json(steps_raw)
        else:
            steps = ()
        return WorkflowState(
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            status=row["state"],
            current_step=row["current_step"],
            created_at=_dt_from_db(row["created_at"]),
            updated_at=_dt_from_db(row["updated_at"]),
            started_at=_dt_from_db(row["started_at"]),
            completed_at=_dt_from_db(row["completed_at"]),
            failed_at=_dt_from_db(row["failed_at"]),
            error_code=row["error_code"],
            version=int(row["version"]),
            steps=steps,
            execution_key=row["execution_key"],
        )
