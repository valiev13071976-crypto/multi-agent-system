import asyncio
import os
import uuid
from dataclasses import replace
from datetime import timedelta

from autonomy.errors import IdempotencyTransitionError
from autonomy.models import (
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_FAILED,
    IDEMPOTENCY_STARTED,
    IDEMPOTENCY_UNCERTAIN,
    utc_now,
)
from workflow.models import TERMINAL_STATUSES

from side_effects.errors import (
    ReconciliationConflictError,
    ReconciliationNotEligibleError,
    ReconciliationNotFoundError,
)
from side_effects.models import (
    ADAPTER_RECON_SUCCEEDED,
    DECISION_MANUAL_REVIEW_REQUIRED,
    DECISION_MARK_COMPLETED,
    DECISION_NO_ACTION,
    DECISION_REAUTHORIZATION_REQUIRED,
    DEFAULT_MAX_RECONCILIATION_ATTEMPTS,
    DEFAULT_RECONCILIATION_BACKOFF_SECONDS,
    DEFAULT_RECONCILIATION_TIMEOUT_SECONDS,
    DEFAULT_STARTED_STALE_AFTER_SECONDS,
    EVENT_MANUAL_RESOLUTION_FAILURE,
    EVENT_MANUAL_RESOLUTION_SUCCESS,
    EVENT_MANUAL_REVIEW_REQUIRED,
    EVENT_RECONCILIATION_CONFIRMED_FAILURE,
    EVENT_RECONCILIATION_CONFIRMED_SUCCESS,
    EVENT_RECONCILIATION_CONFLICT,
    EVENT_RECONCILIATION_CREATED,
    EVENT_RECONCILIATION_LOOKUP_FAILED,
    EVENT_RECONCILIATION_LOOKUP_SUCCEEDED,
    EVENT_RECONCILIATION_STARTED,
    EVENT_RECONCILIATION_STILL_UNCERTAIN,
    EVENT_RECONCILIATION_TIMEOUT,
    EVENT_RECOVERY_RETRY_DENIED,
    EVENT_RECOVERY_RETRY_ELIGIBLE,
    OUTCOME_KNOWN_FAILURE,
    OUTCOME_KNOWN_SUCCESS,
    OUTCOME_UNCERTAIN,
    RECON_CHECKING,
    RECON_CONFIRMED_FAILED,
    RECON_CONFIRMED_SUCCEEDED,
    RECON_MANUAL_REVIEW,
    RECON_PENDING,
    RECON_STILL_UNCERTAIN,
    RECONCILIATION_ACTIVE,
    RECONCILIATION_TERMINAL,
    STATUS_DENIED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    WORKFLOW_RESOLUTION_EXTERNAL_CONFIRMED,
    WORKFLOW_RESOLUTION_MANUAL_FOLLOWUP,
    ReconciliationRecord,
    ReconciliationResult,
    hash_idempotency_key,
)
from side_effects.recovery import RecoveryPolicy
from side_effects.reconciliation_store import InMemoryReconciliationStore


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw or not str(raw).strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw or not str(raw).strip():
        return default
    return float(raw)


class SideEffectReconciliationService:
    """Sole owner of reconciliation policy. Does not invoke side-effect execute."""

    def __init__(
        self,
        *,
        execution_store,
        idempotency,
        registry,
        audit,
        store=None,
        policy: RecoveryPolicy | None = None,
        state_manager=None,
        stale_after_seconds: int | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
    ):
        self.execution_store = execution_store
        self.idempotency = idempotency
        self.registry = registry
        self.audit = audit
        self.store = store or InMemoryReconciliationStore()
        self.policy = policy or RecoveryPolicy()
        self.state_manager = state_manager
        self.stale_after_seconds = (
            DEFAULT_STARTED_STALE_AFTER_SECONDS
            if stale_after_seconds is None
            else stale_after_seconds
        )
        if stale_after_seconds is None:
            self.stale_after_seconds = _env_int(
                "SIDE_EFFECT_STARTED_STALE_AFTER_SECONDS",
                DEFAULT_STARTED_STALE_AFTER_SECONDS,
            )
        self.timeout_seconds = (
            DEFAULT_RECONCILIATION_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        if timeout_seconds is None:
            self.timeout_seconds = _env_float(
                "SIDE_EFFECT_RECONCILIATION_TIMEOUT_SECONDS",
                DEFAULT_RECONCILIATION_TIMEOUT_SECONDS,
            )
        self.max_attempts = (
            DEFAULT_MAX_RECONCILIATION_ATTEMPTS
            if max_attempts is None
            else max_attempts
        )
        self.observability = None
        self.rollback_invocations = 0
        self.observability = None

    def _obs_emit(self, event_type: str, *, workflow_id="", task_id="", tool_id="", operation="", **kwargs):
        from observability.helpers import safe_emit

        if self.observability is None:
            return
        parent = (
            self.observability.context_for_workflow(workflow_id) if workflow_id else None
        )
        span = (
            self.observability.child_span(parent)
            if parent is not None
            else self.observability.create_context(
                workflow_id=workflow_id, task_id=task_id
            )
        )
        safe_emit(
            self.observability,
            event_type,
            context=span,
            component="reconciliation",
            tool_id=tool_id,
            operation=operation,
            **kwargs,
        )

    def get(self, reconciliation_id: str) -> ReconciliationRecord:
        record = self.store.get(reconciliation_id)
        if record is None:
            raise ReconciliationNotFoundError()
        return record

    def list_pending(self):
        return self.store.list_pending()

    def list_manual_review(self):
        return self.store.list_manual_review()

    def is_eligible(self, execution) -> bool:
        if execution.status == STATUS_SUCCEEDED and execution.outcome == OUTCOME_KNOWN_SUCCESS:
            return False
        adapter_started = bool(dict(execution.metadata).get("adapter_started"))
        if execution.status == STATUS_DENIED or (
            execution.outcome == OUTCOME_KNOWN_FAILURE and not adapter_started
        ):
            return False
        if execution.outcome == OUTCOME_UNCERTAIN:
            return True
        idem = self._idempotency_for(execution)
        if idem is not None and idem.state in {IDEMPOTENCY_UNCERTAIN, IDEMPOTENCY_STARTED}:
            return True
        adapter = self.registry.get(execution.tool_id)
        if (
            execution.outcome == OUTCOME_KNOWN_FAILURE
            and adapter is not None
            and bool(getattr(adapter.descriptor, "supports_reconciliation", False))
            and adapter_started
        ):
            return True
        return False

    def create_for_execution(self, execution_id: str, *, reason_code: str = "uncertain") -> ReconciliationRecord:
        execution = self.execution_store.get(execution_id)
        if execution is None:
            raise ReconciliationNotEligibleError("execution_not_found")
        existing = self._active_for_execution(execution_id)
        if existing is not None:
            return existing
        if not self.is_eligible(execution):
            raise ReconciliationNotEligibleError()
        stamp = utc_now()
        record = ReconciliationRecord(
            reconciliation_id=str(uuid.uuid4()),
            execution_id=execution.execution_id,
            workflow_id=execution.workflow_id,
            task_id=execution.task_id,
            action_id=execution.action_id,
            tool_id=execution.tool_id,
            operation=execution.operation,
            idempotency_key_hash=execution.idempotency_key_hash,
            status=RECON_PENDING,
            decision=DECISION_NO_ACTION,
            attempt=0,
            created_at=stamp,
            reason_code=reason_code,
            version=1,
            metadata={"recovery_attempt": execution.recovery_attempt},
        )
        self.store.create(record)
        updated = replace(
            execution,
            reconciliation_id=record.reconciliation_id,
            version=int(execution.version) + 1,
        )
        self.execution_store.save(updated)
        self.audit.record(
            EVENT_RECONCILIATION_CREATED,
            execution_id=execution.execution_id,
            workflow_id=execution.workflow_id,
            action_id=execution.action_id,
            tool_id=execution.tool_id,
            operation=execution.operation,
            reason_code=reason_code,
            metadata={"reconciliation_id": record.reconciliation_id},
        )
        self._obs_emit(
            "reconciliation.created",
            workflow_id=execution.workflow_id,
            task_id=execution.task_id,
            tool_id=execution.tool_id,
            operation=execution.operation,
            status="created",
            metadata={
                "execution_id": execution.execution_id,
                "reconciliation_id": record.reconciliation_id,
            },
        )
        return record

    def find_stale_started(self, now=None):
        stamp = now or utc_now()
        threshold = timedelta(seconds=int(self.stale_after_seconds))
        found = []
        for execution in self.execution_store.list_all():
            if execution.status in {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_DENIED}:
                continue
            started = execution.started_at
            if started is None:
                continue
            if stamp - started < threshold:
                continue
            idem = self._idempotency_for(execution)
            if idem is None or idem.state != IDEMPOTENCY_STARTED:
                if execution.outcome != OUTCOME_UNCERTAIN:
                    continue
            found.append(execution)
        return tuple(found)

    async def reconcile(
        self,
        reconciliation_id: str,
        *,
        action=None,
        now=None,
        expected_version: int | None = None,
        context=None,
    ) -> ReconciliationResult:
        record = self.get(reconciliation_id)
        if expected_version is not None and record.version != expected_version:
            raise ReconciliationConflictError("stale_version")
        if record.status in RECONCILIATION_TERMINAL:
            raise ReconciliationConflictError("already_resolved")
        stamp = now or utc_now()
        record = self._save(
            replace(
                record,
                status=RECON_CHECKING,
                started_at=record.started_at or stamp,
                last_checked_at=stamp,
                attempt=record.attempt + 1,
                version=record.version + 1,
            )
        )
        self.audit.record(
            EVENT_RECONCILIATION_STARTED,
            execution_id=record.execution_id,
            workflow_id=record.workflow_id,
            action_id=record.action_id,
            tool_id=record.tool_id,
            metadata={"reconciliation_id": record.reconciliation_id, "attempt": record.attempt},
        )
        execution = self.execution_store.get(record.execution_id)
        adapter = self.registry.get(record.tool_id)
        descriptor = getattr(adapter, "descriptor", None)
        supports = bool(descriptor and descriptor.supports_reconciliation)
        if adapter is None or not supports or not hasattr(adapter, "reconcile"):
            return self._finish_manual(
                record,
                execution,
                stamp,
                "reconciliation_unsupported",
            )
        lookup = None
        try:
            lookup = await asyncio.wait_for(
                adapter.reconcile(execution, action, context),
                timeout=float(self.timeout_seconds),
            )
        except asyncio.TimeoutError:
            self.audit.record(
                EVENT_RECONCILIATION_TIMEOUT,
                execution_id=record.execution_id,
                workflow_id=record.workflow_id,
                action_id=record.action_id,
                reason_code="reconciliation_timeout",
            )
            decision = self.policy.decide(
                lookup_status=None,
                authoritative=bool(descriptor.reconciliation_authoritative),
                not_found_is_failure=bool(descriptor.not_found_is_authoritative_failure),
                reversible=bool(descriptor.reversible),
                supports_idempotency=bool(descriptor.supports_idempotency),
                attempts=record.attempt,
                max_attempts=self.max_attempts,
                supports_reconciliation=True,
            )
            decision["reason_code"] = "reconciliation_timeout"
            return self._apply_unknown(record, execution, stamp, decision)
        except Exception:
            self.audit.record(
                EVENT_RECONCILIATION_LOOKUP_FAILED,
                execution_id=record.execution_id,
                workflow_id=record.workflow_id,
                action_id=record.action_id,
                reason_code="lookup_failed",
            )
            decision = self.policy.decide(
                lookup_status=None,
                authoritative=bool(descriptor.reconciliation_authoritative),
                not_found_is_failure=bool(descriptor.not_found_is_authoritative_failure),
                reversible=bool(descriptor.reversible),
                supports_idempotency=bool(descriptor.supports_idempotency),
                attempts=record.attempt,
                max_attempts=self.max_attempts,
                supports_reconciliation=True,
            )
            return self._apply_unknown(record, execution, stamp, decision)

        self.audit.record(
            EVENT_RECONCILIATION_LOOKUP_SUCCEEDED,
            execution_id=record.execution_id,
            workflow_id=record.workflow_id,
            action_id=record.action_id,
            tool_id=record.tool_id,
            metadata={"lookup_status": lookup.status},
        )
        self._obs_emit(
            "reconciliation.checked",
            workflow_id=record.workflow_id,
            task_id=record.task_id,
            tool_id=record.tool_id,
            operation=record.operation,
            status="checked",
            metadata={
                "execution_id": record.execution_id,
                "lookup_status": getattr(lookup, "status", None),
            },
        )
        if (
            execution.external_reference
            and lookup.external_reference
            and execution.external_reference != lookup.external_reference
        ):
            self.audit.record(
                EVENT_RECONCILIATION_CONFLICT,
                execution_id=record.execution_id,
                workflow_id=record.workflow_id,
                action_id=record.action_id,
                reason_code="external_reference_conflict",
            )
            return self._finish_manual(
                record,
                execution,
                stamp,
                "external_reference_conflict",
            )
        decision = self.policy.decide(
            lookup_status=lookup.status,
            authoritative=bool(descriptor.reconciliation_authoritative),
            not_found_is_failure=bool(descriptor.not_found_is_authoritative_failure),
            reversible=bool(lookup.reversible if lookup.reversible is not None else descriptor.reversible),
            supports_idempotency=bool(descriptor.supports_idempotency),
            attempts=record.attempt,
            max_attempts=self.max_attempts,
            supports_reconciliation=True,
        )
        if decision["reason_code"] == "confirmed_succeeded":
            return self._apply_success(record, execution, stamp, lookup, decision)
        if decision["reason_code"] == "confirmed_failed":
            return self._apply_failure(record, execution, stamp, lookup, decision)
        if decision["manual_review_required"]:
            return self._finish_manual(record, execution, stamp, decision["reason_code"])
        return self._apply_unknown(record, execution, stamp, decision)

    def resolve_manual(
        self,
        reconciliation_id: str,
        *,
        outcome: str,
        resolver_id: str,
        reason_code: str,
        expected_version: int | None = None,
        now=None,
    ) -> ReconciliationResult:
        if not str(resolver_id or "").strip() or not str(reason_code or "").strip():
            raise ReconciliationConflictError("resolver_and_reason_required")
        record = self.get(reconciliation_id)
        if expected_version is not None and record.version != expected_version:
            raise ReconciliationConflictError("stale_version")
        if record.status in RECONCILIATION_TERMINAL and record.status != RECON_MANUAL_REVIEW:
            raise ReconciliationConflictError("already_resolved")
        stamp = now or utc_now()
        execution = self.execution_store.get(record.execution_id)
        if outcome == "confirm_succeeded":
            lookup_like = type("L", (), {"external_reference": execution.external_reference, "rollback_reference": execution.rollback_reference, "reversible": True, "status": ADAPTER_RECON_SUCCEEDED})()
            decision = {
                "decision": DECISION_MARK_COMPLETED,
                "retry_eligible": False,
                "reauthorization_required": True,
                "rollback_candidate": bool(execution.rollback_reference),
                "manual_review_required": False,
                "reason_code": "manual_confirm_succeeded",
            }
            result = self._apply_success(record, execution, stamp, lookup_like, decision, resolver_id=resolver_id)
            self.audit.record(
                EVENT_MANUAL_RESOLUTION_SUCCESS,
                execution_id=record.execution_id,
                workflow_id=record.workflow_id,
                action_id=record.action_id,
                reason_code=reason_code,
                metadata={"resolver_id": resolver_id},
            )
            return result
        if outcome == "confirm_failed":
            decision = {
                "decision": DECISION_REAUTHORIZATION_REQUIRED,
                "retry_eligible": True,
                "reauthorization_required": True,
                "rollback_candidate": False,
                "manual_review_required": False,
                "reason_code": "manual_confirm_failed",
            }
            result = self._apply_failure(record, execution, stamp, None, decision, resolver_id=resolver_id)
            self.audit.record(
                EVENT_MANUAL_RESOLUTION_FAILURE,
                execution_id=record.execution_id,
                workflow_id=record.workflow_id,
                action_id=record.action_id,
                reason_code=reason_code,
                metadata={"resolver_id": resolver_id},
            )
            return result
        if outcome == "keep_uncertain":
            record = self._save(
                replace(
                    record,
                    status=RECON_STILL_UNCERTAIN,
                    decision=DECISION_NO_ACTION,
                    resolver_id=resolver_id,
                    reason_code=reason_code,
                    last_checked_at=stamp,
                    version=record.version + 1,
                    metadata={**dict(record.metadata), "resolver_id": resolver_id},
                )
            )
            return self._result(record, execution, stamp, {
                "decision": DECISION_NO_ACTION,
                "retry_eligible": False,
                "reauthorization_required": True,
                "rollback_candidate": False,
                "manual_review_required": False,
                "reason_code": reason_code,
            })
        raise ReconciliationConflictError("unknown_manual_outcome")

    def _apply_success(self, record, execution, stamp, lookup, decision, resolver_id=None):
        ext = getattr(lookup, "external_reference", None) or execution.external_reference
        rollback_ref = getattr(lookup, "rollback_reference", None) or execution.rollback_reference
        self._transition_idempotency(execution, IDEMPOTENCY_COMPLETED)
        updated_exec = replace(
            execution,
            status=STATUS_SUCCEEDED,
            outcome=OUTCOME_KNOWN_SUCCESS,
            error_code=None,
            completed_at=stamp,
            external_reference=ext,
            rollback_reference=rollback_ref,
            reconciliation_id=record.reconciliation_id,
            version=int(execution.version) + 1,
        )
        self.execution_store.save(updated_exec)
        record = self._save(
            replace(
                record,
                status=RECON_CONFIRMED_SUCCEEDED,
                decision=decision["decision"],
                completed_at=stamp,
                last_checked_at=stamp,
                external_reference=ext,
                reason_code=decision["reason_code"],
                resolver_id=resolver_id,
                version=record.version + 1,
            )
        )
        self.audit.record(
            EVENT_RECONCILIATION_CONFIRMED_SUCCESS,
            execution_id=record.execution_id,
            workflow_id=record.workflow_id,
            action_id=record.action_id,
            reason_code=decision["reason_code"],
            metadata={"external_reference": ext},
        )
        self._obs_emit(
            "reconciliation.completed",
            workflow_id=record.workflow_id,
            task_id=record.task_id,
            tool_id=record.tool_id,
            operation=record.operation,
            status="completed",
            metadata={
                "execution_id": record.execution_id,
                "outcome": "confirmed_success",
            },
        )
        result = self._result(
            record,
            updated_exec,
            stamp,
            decision,
            outcome=OUTCOME_KNOWN_SUCCESS,
            workflow_resolution=WORKFLOW_RESOLUTION_EXTERNAL_CONFIRMED,
        )
        return result

    def _apply_failure(self, record, execution, stamp, lookup, decision, resolver_id=None):
        self._transition_idempotency(execution, IDEMPOTENCY_FAILED)
        updated_exec = replace(
            execution,
            status=STATUS_FAILED,
            outcome=OUTCOME_KNOWN_FAILURE,
            completed_at=stamp,
            reconciliation_id=record.reconciliation_id,
            error_code=execution.error_code or "confirmed_failed",
            version=int(execution.version) + 1,
        )
        self.execution_store.save(updated_exec)
        record = self._save(
            replace(
                record,
                status=RECON_CONFIRMED_FAILED,
                decision=decision["decision"],
                completed_at=stamp,
                last_checked_at=stamp,
                reason_code=decision["reason_code"],
                resolver_id=resolver_id,
                version=record.version + 1,
            )
        )
        event = EVENT_RECONCILIATION_CONFIRMED_FAILURE
        self.audit.record(
            event,
            execution_id=record.execution_id,
            workflow_id=record.workflow_id,
            action_id=record.action_id,
            reason_code=decision["reason_code"],
        )
        if decision.get("retry_eligible"):
            self.audit.record(
                EVENT_RECOVERY_RETRY_ELIGIBLE,
                execution_id=record.execution_id,
                workflow_id=record.workflow_id,
                action_id=record.action_id,
                reason_code="reauthorization_required",
            )
        else:
            self.audit.record(
                EVENT_RECOVERY_RETRY_DENIED,
                execution_id=record.execution_id,
                workflow_id=record.workflow_id,
                action_id=record.action_id,
                reason_code="retry_denied",
            )
        lineage = self.policy.lineage(execution.execution_id, record.reconciliation_id, 1)
        result = self._result(
            record,
            updated_exec,
            stamp,
            decision,
            outcome=OUTCOME_KNOWN_FAILURE,
            workflow_resolution=WORKFLOW_RESOLUTION_MANUAL_FOLLOWUP,
        )
        return replace(
            result,
            recovery_id=lineage.recovery_id,
            metadata={
                **dict(result.metadata),
                "parent_execution_id": execution.execution_id,
                "recovery_attempt": 1,
            },
        )

    def _apply_unknown(self, record, execution, stamp, decision):
        if decision.get("manual_review_required"):
            return self._finish_manual(record, execution, stamp, decision["reason_code"])
        delay = DEFAULT_RECONCILIATION_BACKOFF_SECONDS * (2 ** max(record.attempt - 1, 0))
        record = self._save(
            replace(
                record,
                status=RECON_STILL_UNCERTAIN,
                decision=DECISION_NO_ACTION,
                last_checked_at=stamp,
                next_check_at=stamp + timedelta(seconds=delay),
                reason_code=decision["reason_code"],
                version=record.version + 1,
            )
        )
        self.audit.record(
            EVENT_RECONCILIATION_STILL_UNCERTAIN,
            execution_id=record.execution_id,
            workflow_id=record.workflow_id,
            action_id=record.action_id,
            reason_code=decision["reason_code"],
        )
        return self._result(record, execution, stamp, decision, outcome=OUTCOME_UNCERTAIN)

    def _finish_manual(self, record, execution, stamp, reason_code: str):
        record = self._save(
            replace(
                record,
                status=RECON_MANUAL_REVIEW,
                decision=DECISION_MANUAL_REVIEW_REQUIRED,
                completed_at=stamp,
                last_checked_at=stamp,
                reason_code=reason_code,
                version=record.version + 1,
            )
        )
        self.audit.record(
            EVENT_MANUAL_REVIEW_REQUIRED,
            execution_id=record.execution_id,
            workflow_id=record.workflow_id,
            action_id=record.action_id,
            reason_code=reason_code,
        )
        self._obs_emit(
            "reconciliation.manual_review",
            workflow_id=record.workflow_id,
            task_id=record.task_id,
            tool_id=record.tool_id,
            operation=record.operation,
            status="manual_review",
            error_code=reason_code,
            metadata={"execution_id": record.execution_id},
        )
        return self._result(
            record,
            execution,
            stamp,
            {
                "decision": DECISION_MANUAL_REVIEW_REQUIRED,
                "retry_eligible": False,
                "reauthorization_required": True,
                "rollback_candidate": False,
                "manual_review_required": True,
                "reason_code": reason_code,
            },
            outcome=execution.outcome if execution else OUTCOME_UNCERTAIN,
            workflow_resolution=WORKFLOW_RESOLUTION_MANUAL_FOLLOWUP,
        )

    def _result(self, record, execution, stamp, decision, outcome=None, workflow_resolution=None):
        return ReconciliationResult(
            reconciliation_id=record.reconciliation_id,
            execution_id=record.execution_id,
            status=record.status,
            decision=decision["decision"],
            outcome=outcome if outcome is not None else (execution.outcome if execution else None),
            external_reference=record.external_reference or (execution.external_reference if execution else None),
            retry_eligible=bool(decision.get("retry_eligible")),
            reauthorization_required=bool(decision.get("reauthorization_required")),
            rollback_candidate=bool(decision.get("rollback_candidate")),
            manual_review_required=bool(decision.get("manual_review_required")),
            reason_code=decision["reason_code"],
            checked_at=stamp,
            workflow_resolution=workflow_resolution,
            metadata={
                "workflow_terminal": self._workflow_still_terminal(execution.workflow_id if execution else None),
            },
        )

    def _transition_idempotency(self, execution, target: str) -> None:
        key = self._raw_idempotency_key(execution)
        if not key:
            return
        existing = self.idempotency.get(key)
        if existing is None:
            return
        if existing.state == target:
            return
        try:
            if existing.state == IDEMPOTENCY_STARTED and target in {
                IDEMPOTENCY_COMPLETED,
                IDEMPOTENCY_FAILED,
            }:
                self.idempotency.reconcile_transition(key, IDEMPOTENCY_UNCERTAIN)
            self.idempotency.reconcile_transition(key, target)
        except IdempotencyTransitionError:
            raise ReconciliationConflictError("idempotency_transition_conflict")

    def _raw_idempotency_key(self, execution) -> str | None:
        items = getattr(self.idempotency.store, "_items", {})
        for row in items.values():
            if hash_idempotency_key(row.key) == execution.idempotency_key_hash:
                return row.key
        return None

    def _idempotency_for(self, execution):
        key = self._raw_idempotency_key(execution)
        if not key:
            return None
        return self.idempotency.get(key)

    def _active_for_execution(self, execution_id: str):
        rows = self.store.find_by_execution(execution_id)
        for row in rows:
            if row.status in RECONCILIATION_ACTIVE or row.status == RECON_CHECKING:
                return row
        return rows[0] if rows else None

    def _save(self, record: ReconciliationRecord) -> ReconciliationRecord:
        return self.store.save(record)

    def _workflow_still_terminal(self, workflow_id: str | None) -> bool:
        if self.state_manager is None or not workflow_id:
            return False
        try:
            state = self.state_manager.get(workflow_id)
        except Exception:
            return False
        return state.status in TERMINAL_STATUSES
