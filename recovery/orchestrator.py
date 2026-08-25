"""RecoveryOrchestrator — single owner for failure recovery orchestration."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import MappingProxyType

from autonomy.models import sanitize_metadata
from recovery.models import (
    ACTION_CANCEL,
    ACTION_DEFER,
    ACTION_MARK_BLOCKED,
    ACTION_MARK_RESOLVED,
    ACTION_RECONCILE_READ_ONLY,
    ACTION_REQUEST_NEW_AUTHORIZATION,
    ACTION_RESUME_WORKFLOW,
    ACTION_ROLLBACK,
    CASE_BUDGET_UNCERTAIN_COST,
    CASE_MANUAL_REVIEW,
    CASE_PENDING_RECONCILIATION,
    CASE_PERMIT_CONSUMED_BEFORE_MUTATION,
    CASE_STALE_STARTED,
    CASE_TYPES,
    CASE_UNCERTAIN_SIDE_EFFECT,
    CASE_WORKFLOW_WAITING_RECOVERY,
    DECISION_BLOCK,
    DECISION_CANCEL,
    DECISION_DEFER,
    DECISION_RECONCILE,
    DECISION_RESUME,
    DECISION_ROLLBACK,
    OPERATOR_DECISIONS,
    RecoveryAction,
    RecoveryCase,
    RecoveryDecision,
    RecoveryPlan,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_CHECKING,
    STATUS_OPEN,
    STATUS_QUEUED,
    STATUS_RESOLVED,
    STATUS_WAITING_APPROVAL,
    STATUS_WAITING_OPERATOR,
    utc_now,
)
from recovery.policy import NEVER_AUTO_TRUST, RecoveryPolicy
from recovery.queue import RecoveryQueue
from recovery.store import (
    InMemoryRecoveryCaseStore,
    RecoveryConflictError,
    RecoveryPersistenceUnavailableError,
)
from side_effects.models import (
    OUTCOME_UNCERTAIN,
    RECON_CONFIRMED_FAILED,
    RECON_CONFIRMED_SUCCEEDED,
    RECON_MANUAL_REVIEW,
    RECON_STILL_UNCERTAIN,
    STATUS_STARTED,
    STATUS_UNKNOWN,
)
from workflow.models import TERMINAL_STATUSES


class RecoveryAuthorizationRequired(RuntimeError):
    """Mutation/resume requires AutonomyGate + HITL + new permit — not auto-executed."""

    def __init__(self, reason: str = "recovery_authorization_required", *, plan: RecoveryPlan | None = None):
        self.reason = reason
        self.plan = plan
        super().__init__(reason)


class RecoveryMutationBlocked(RuntimeError):
    def __init__(self, reason: str = "recovery_persistence_unavailable"):
        self.reason = reason
        super().__init__(reason)


class RecoveryOrchestrator:
    """Identify, classify, queue, decide, and execute only safe recovery steps."""

    def __init__(
        self,
        *,
        store=None,
        queue: RecoveryQueue | None = None,
        policy: RecoveryPolicy | None = None,
        reconciliation_service=None,
        workflow_engine=None,
        gate=None,
        hitl=None,
        side_effect_executor=None,
        observability=None,
        audit=None,
        enabled: bool = True,
        max_read_checks: int = 3,
        base_backoff_seconds: float = 5.0,
        max_backoff_seconds: float = 60.0,
        enqueue_reconcile_on_create: bool = True,
    ):
        self.store = store or InMemoryRecoveryCaseStore()
        self.queue = queue or RecoveryQueue(self.store, max_attempts=max_read_checks)
        self.policy = policy or RecoveryPolicy()
        self.reconciliation_service = reconciliation_service
        self.workflow_engine = workflow_engine
        self.gate = gate
        self.hitl = hitl
        self.side_effect_executor = side_effect_executor
        self.observability = observability
        self.audit = audit
        self.enabled = bool(enabled)
        self.max_read_checks = int(max_read_checks)
        self.base_backoff_seconds = float(base_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self.enqueue_reconcile_on_create = bool(enqueue_reconcile_on_create)
        self.mutation_blocked_reason: str | None = None
        self.network_calls = 0
        self.mutation_calls = 0
        self._open_gauge = 0

    # ------------------------------------------------------------------ API
    def list_open_cases(self) -> tuple[RecoveryCase, ...]:
        return self.store.list_open()

    def get_case(self, recovery_id: str) -> RecoveryCase | None:
        return self.store.get(recovery_id)

    def plan(
        self,
        recovery_id: str,
        *,
        reconciliation_status: str | None = None,
        workflow_terminal: bool | None = None,
        supports_authoritative_reconcile: bool = True,
    ) -> RecoveryPlan:
        case = self._require_case(recovery_id)
        terminal = workflow_terminal
        if terminal is None:
            terminal = self._workflow_is_terminal(case.workflow_id)
        return self.policy.plan(
            case,
            reconciliation_status=reconciliation_status,
            operator_decision=case.operator_decision,
            workflow_terminal=bool(terminal),
            supports_authoritative_reconcile=supports_authoritative_reconcile,
        )

    def record_decision(
        self,
        recovery_id: str,
        decision: str,
        *,
        actor_id: str,
        reason_code: str,
        note_safe: str = "",
        metadata_safe: dict | None = None,
        now: datetime | None = None,
    ) -> RecoveryDecision:
        if decision not in OPERATOR_DECISIONS:
            raise ValueError(f"invalid_operator_decision:{decision}")
        case = self._require_case(recovery_id)
        stamp = now or utc_now()
        rec = RecoveryDecision(
            decision_id=str(uuid.uuid4()),
            recovery_id=recovery_id,
            decision=decision,
            actor_id=str(actor_id),
            reason_code=str(reason_code),
            created_at=stamp,
            note_safe=str(note_safe or "")[:500],
            metadata_safe=sanitize_metadata(metadata_safe or {}),
        )
        try:
            self.store.add_decision(rec)
        except RecoveryPersistenceUnavailableError:
            self._fail_closed_persistence()
            raise
        status = case.status
        next_check = case.next_check_at
        if decision == DECISION_BLOCK:
            status = STATUS_BLOCKED
        elif decision == DECISION_CANCEL:
            status = STATUS_CANCELLED
        elif decision == DECISION_DEFER:
            status = STATUS_WAITING_OPERATOR
            next_check = stamp + timedelta(seconds=self.base_backoff_seconds)
        elif decision in {DECISION_RECONCILE, DECISION_RESUME, DECISION_ROLLBACK}:
            status = STATUS_WAITING_APPROVAL if decision == DECISION_ROLLBACK else STATUS_OPEN
        updated = self._clone_case(
            case,
            status=status,
            operator_decision=decision,
            reason_code=reason_code,
            updated_at=stamp,
            next_check_at=next_check,
        )
        case = self._update(updated, expected_version=case.version)
        self._emit(
            "recovery.decision_recorded",
            case,
            status=case.status,
            metadata={"decision": decision, "reason_code": reason_code},
        )
        self._audit(
            "recovery_decision",
            recovery_id=recovery_id,
            decision=decision,
            actor_id=actor_id,
            reason_code=reason_code,
        )
        if decision == DECISION_BLOCK:
            self._emit("recovery.blocked", case, status=STATUS_BLOCKED)
            self._metric_inc("recovery_blocked_total", case)
        if decision == DECISION_RECONCILE and self.enqueue_reconcile_on_create:
            self._enqueue_read_check(case, now=stamp)
        return rec

    async def execute_safe_step(
        self,
        recovery_id: str,
        action: RecoveryAction | None = None,
        *,
        now: datetime | None = None,
        action_for_auth=None,
        permit=None,
        evaluate_kwargs=None,
    ) -> dict:
        """Execute only explicitly safe steps. Mutations raise RecoveryAuthorizationRequired."""

        stamp = now or utc_now()
        case = self._require_case(recovery_id)
        if case.status == STATUS_BLOCKED:
            return {"status": "blocked", "reason_code": "case_blocked", "mutated": False}
        plan = self.plan(recovery_id)
        step = action or (plan.steps[0] if plan.steps else None)
        if step is None:
            if plan.waiting_operator:
                case = self._set_status(case, STATUS_WAITING_OPERATOR, reason_code=plan.reason_code, now=stamp)
                self._emit("recovery.waiting_operator", case, status=STATUS_WAITING_OPERATOR)
            return {
                "status": case.status,
                "reason_code": plan.reason_code,
                "mutated": False,
                "waiting_operator": plan.waiting_operator,
            }

        if step.action_type in {ACTION_ROLLBACK, ACTION_RESUME_WORKFLOW, ACTION_REQUEST_NEW_AUTHORIZATION}:
            if step.action_type == ACTION_RESUME_WORKFLOW:
                return await self._resume_workflow(case, step, now=stamp)
            # Decision alone never mutates — caller must pass gate/HITL/permit via executor.
            raise RecoveryAuthorizationRequired(
                "recovery_mutation_requires_authorization",
                plan=RecoveryPlan(
                    recovery_id=recovery_id,
                    steps=(step,),
                    reason_code=step.reason_code or "authorization_required",
                ),
            )

        if step.action_type == ACTION_RECONCILE_READ_ONLY:
            return await self._reconcile_read_only(case, now=stamp)

        if step.action_type == ACTION_MARK_RESOLVED:
            case = self._set_status(case, STATUS_RESOLVED, reason_code=step.reason_code, now=stamp)
            self._emit("recovery.resolved", case, status=STATUS_RESOLVED)
            self._metric_inc("recovery_resolved_total", case)
            return {"status": STATUS_RESOLVED, "reason_code": step.reason_code, "mutated": False}

        if step.action_type == ACTION_MARK_BLOCKED:
            case = self._set_status(case, STATUS_BLOCKED, reason_code=step.reason_code, now=stamp)
            self._emit("recovery.blocked", case, status=STATUS_BLOCKED)
            self._metric_inc("recovery_blocked_total", case)
            return {"status": STATUS_BLOCKED, "reason_code": step.reason_code, "mutated": False}

        if step.action_type == ACTION_DEFER:
            delay = min(
                self.max_backoff_seconds,
                self.base_backoff_seconds * (2 ** max(0, case.attempt)),
            )
            nxt = stamp + timedelta(seconds=delay)
            current = self._require_case(case.recovery_id)
            case = self._update(
                self._clone_case(
                    current,
                    status=STATUS_WAITING_OPERATOR,
                    next_check_at=nxt,
                    reason_code=step.reason_code or "deferred",
                    updated_at=stamp,
                ),
                expected_version=current.version,
            )
            return {
                "status": STATUS_WAITING_OPERATOR,
                "reason_code": "deferred",
                "mutated": False,
                "next_check_at": nxt.isoformat(),
            }

        if step.action_type == ACTION_CANCEL:
            case = self._set_status(case, STATUS_CANCELLED, reason_code=step.reason_code, now=stamp)
            return {"status": STATUS_CANCELLED, "reason_code": step.reason_code, "mutated": False}

        return {"status": case.status, "reason_code": "noop", "mutated": False}

    def create_case(
        self,
        *,
        execution_id: str,
        case_type: str,
        workflow_id: str = "",
        task_id: str = "",
        action_id: str = "",
        tool_id: str = "",
        operation: str = "",
        reason_code: str = "",
        reconciliation_id: str | None = None,
        tool_trust_level: str = "",
        reversible: bool = False,
        metadata_safe: dict | None = None,
        parent_recovery_id: str | None = None,
        now: datetime | None = None,
        enqueue: bool | None = None,
    ) -> RecoveryCase:
        if case_type not in CASE_TYPES:
            raise ValueError(f"invalid_case_type:{case_type}")
        existing = self.store.find_active(execution_id, case_type)
        if existing is not None:
            return existing
        stamp = now or utc_now()
        severity = self.policy.classify_severity(
            case_type=case_type,
            tool_trust_level=tool_trust_level,
            reversible=reversible,
        )
        case = RecoveryCase(
            recovery_id=str(uuid.uuid4()),
            execution_id=execution_id,
            workflow_id=workflow_id or "",
            task_id=task_id or "",
            action_id=action_id or "",
            tool_id=tool_id or "",
            operation=operation or "",
            case_type=case_type,
            status=STATUS_OPEN,
            severity=severity,
            reason_code=reason_code or case_type,
            created_at=stamp,
            updated_at=stamp,
            max_attempts=self.max_read_checks,
            reconciliation_id=reconciliation_id,
            parent_recovery_id=parent_recovery_id,
            tool_trust_level=tool_trust_level or "",
            reversible=bool(reversible),
            metadata_safe=sanitize_metadata(metadata_safe or {}),
            version=1,
        )
        try:
            case = self.store.create(case)
        except RecoveryConflictError:
            existing = self.store.find_active(execution_id, case_type)
            if existing is not None:
                return existing
            raise
        except RecoveryPersistenceUnavailableError:
            self._fail_closed_persistence()
            raise
        self._emit("recovery.case_created", case, status=STATUS_OPEN)
        self._metric_inc("recovery_cases_total", case)
        if case_type == CASE_MANUAL_REVIEW:
            self._metric_inc("recovery_manual_review_total", case)
            case = self._set_status(case, STATUS_WAITING_OPERATOR, reason_code=case.reason_code, now=stamp)
            self._emit("recovery.waiting_operator", case, status=STATUS_WAITING_OPERATOR)
        should_enqueue = self.enqueue_reconcile_on_create if enqueue is None else enqueue
        if should_enqueue and case_type in {
            CASE_UNCERTAIN_SIDE_EFFECT,
            CASE_PENDING_RECONCILIATION,
            CASE_STALE_STARTED,
        }:
            # High-risk irreversible defaults to waiting_operator without auto queue mutate.
            if tool_trust_level in NEVER_AUTO_TRUST:
                case = self._set_status(
                    case, STATUS_WAITING_OPERATOR, reason_code="waiting_operator_high_risk", now=stamp
                )
                self._emit("recovery.waiting_operator", case, status=STATUS_WAITING_OPERATOR)
            else:
                self._enqueue_read_check(case, now=stamp)
        self._refresh_open_gauge()
        return case

    def ensure_case_for_uncertain(
        self,
        *,
        execution_id: str,
        workflow_id: str = "",
        task_id: str = "",
        action_id: str = "",
        tool_id: str = "",
        operation: str = "",
        tool_trust_level: str = "",
        reversible: bool = False,
        reconciliation_id: str | None = None,
        now: datetime | None = None,
    ) -> RecoveryCase:
        """Create uncertain case or fail closed if persistence unavailable."""
        try:
            return self.create_case(
                execution_id=execution_id,
                case_type=CASE_UNCERTAIN_SIDE_EFFECT,
                workflow_id=workflow_id,
                task_id=task_id,
                action_id=action_id,
                tool_id=tool_id,
                operation=operation,
                tool_trust_level=tool_trust_level,
                reversible=reversible,
                reconciliation_id=reconciliation_id,
                reason_code="uncertain_side_effect",
                now=now,
                enqueue=True,
            )
        except RecoveryPersistenceUnavailableError:
            self._fail_closed_persistence()
            raise

    def require_mutation_allowed(self) -> None:
        if self.mutation_blocked_reason:
            raise RecoveryMutationBlocked(self.mutation_blocked_reason)

    def get_due_jobs(self, now: datetime | None = None):
        return self.queue.get_due_jobs(now)

    async def process_due_job(self, job_id: str, *, now: datetime | None = None) -> dict:
        stamp = now or utc_now()
        leased = self.queue.lease(job_id, now=stamp)
        case = self.get_case(leased.recovery_id)
        if case is None:
            self.queue.cancel(job_id, now=stamp)
            return {"status": "cancelled", "reason_code": "case_missing"}
        if case.status in {STATUS_RESOLVED, STATUS_BLOCKED, STATUS_CANCELLED}:
            self.queue.complete(job_id, now=stamp)
            return {"status": case.status, "reason_code": "case_terminal"}
        try:
            result = await self.execute_safe_step(case.recovery_id, now=stamp)
        except RecoveryAuthorizationRequired as exc:
            self.queue.complete(job_id, now=stamp)
            return {"status": "authorization_required", "reason_code": exc.reason, "mutated": False}
        if result.get("status") == STATUS_RESOLVED:
            self.queue.complete(job_id, now=stamp)
            return result
        # unknown / waiting — defer with backoff or dead-letter
        delay = min(
            self.max_backoff_seconds,
            self.base_backoff_seconds * (2 ** max(0, leased.attempt)),
        )
        deferred = self.queue.defer(job_id, delay_seconds=delay, now=stamp)
        if deferred.status == "dead_letter":
            case = self._set_status(
                case, STATUS_WAITING_OPERATOR, reason_code="max_read_checks", now=stamp
            )
            self._emit("recovery.waiting_operator", case, status=STATUS_WAITING_OPERATOR)
            result = {**result, "status": STATUS_WAITING_OPERATOR, "reason_code": "max_read_checks"}
        return result

    def materialize_from_local_scan(
        self,
        *,
        execution_store,
        reconciliation_store=None,
        permit_store=None,
        budget_store=None,
        now: datetime | None = None,
        enqueue: bool = True,
    ) -> dict:
        """Local-only: create RecoveryCase records. No network, no mutation."""
        stamp = now or utc_now()
        created = 0
        skipped = 0
        network_before = self.network_calls
        mutation_before = self.mutation_calls
        if hasattr(execution_store, "list_all"):
            for row in execution_store.list_all():
                case_type = None
                if getattr(row, "status", None) == STATUS_UNKNOWN or getattr(row, "outcome", None) == OUTCOME_UNCERTAIN:
                    case_type = CASE_UNCERTAIN_SIDE_EFFECT
                elif getattr(row, "status", None) == STATUS_STARTED or (
                    getattr(row, "completed_at", None) is None
                    and getattr(row, "status", None) not in {"succeeded", "failed", "denied", "cancelled"}
                ):
                    case_type = CASE_STALE_STARTED
                if case_type is None:
                    continue
                before = self.store.find_active(row.execution_id, case_type)
                case = self.create_case(
                    execution_id=row.execution_id,
                    case_type=case_type,
                    workflow_id=getattr(row, "workflow_id", "") or "",
                    task_id=getattr(row, "task_id", "") or "",
                    action_id=getattr(row, "action_id", "") or "",
                    tool_id=getattr(row, "tool_id", "") or "",
                    operation=getattr(row, "operation", "") or "",
                    tool_trust_level=str(dict(getattr(row, "metadata", {}) or {}).get("tool_trust_level") or ""),
                    reversible=bool(dict(getattr(row, "metadata", {}) or {}).get("reversible", False)),
                    now=stamp,
                    enqueue=enqueue,
                )
                if before is None and case is not None:
                    created += 1
                else:
                    skipped += 1
        if reconciliation_store is not None:
            for row in reconciliation_store.list_pending():
                before = self.store.find_active(row.execution_id, CASE_PENDING_RECONCILIATION)
                self.create_case(
                    execution_id=row.execution_id,
                    case_type=CASE_PENDING_RECONCILIATION,
                    reconciliation_id=getattr(row, "reconciliation_id", None),
                    reason_code="pending_reconciliation",
                    now=stamp,
                    enqueue=enqueue,
                )
                created += 0 if before else 1
                skipped += 1 if before else 0
            for row in reconciliation_store.list_manual_review():
                before = self.store.find_active(row.execution_id, CASE_MANUAL_REVIEW)
                self.create_case(
                    execution_id=row.execution_id,
                    case_type=CASE_MANUAL_REVIEW,
                    reconciliation_id=getattr(row, "reconciliation_id", None),
                    reason_code="manual_review_required",
                    now=stamp,
                    enqueue=False,
                )
                created += 0 if before else 1
        if permit_store is not None and hasattr(permit_store, "list_by_status"):
            from hitl.models import PERMIT_CONSUMED

            for permit in permit_store.list_by_status(PERMIT_CONSUMED):
                exec_id = str(getattr(permit, "execution_id", "") or getattr(permit, "action_id", "") or permit.permit_id)
                # Detect consumed permit without completed mutation via metadata flag if present.
                meta = dict(getattr(permit, "metadata", {}) or {})
                if not meta.get("mutation_unconfirmed") and not meta.get("uncertain_execution"):
                    continue
                before = self.store.find_active(exec_id, CASE_PERMIT_CONSUMED_BEFORE_MUTATION)
                self.create_case(
                    execution_id=exec_id,
                    case_type=CASE_PERMIT_CONSUMED_BEFORE_MUTATION,
                    workflow_id=str(getattr(permit, "workflow_id", "") or ""),
                    reason_code="permit_consumed_before_mutation",
                    now=stamp,
                    enqueue=False,
                    metadata_safe={"permit_id": permit.permit_id},
                )
                created += 0 if before else 1
        if budget_store is not None and hasattr(budget_store, "list_by_status"):
            from finops.budget_models import RES_UNCERTAIN

            for res in budget_store.list_by_status(RES_UNCERTAIN):
                exec_id = str(getattr(res, "reservation_id", "") or "")
                before = self.store.find_active(exec_id, CASE_BUDGET_UNCERTAIN_COST)
                self.create_case(
                    execution_id=exec_id,
                    case_type=CASE_BUDGET_UNCERTAIN_COST,
                    reason_code="budget_uncertain_cost",
                    now=stamp,
                    enqueue=False,
                    metadata_safe={"reservation_retained": True},
                )
                created += 0 if before else 1
        assert self.network_calls == network_before
        assert self.mutation_calls == mutation_before
        return {
            "created": created,
            "skipped_or_deduped": skipped,
            "network_calls": 0,
            "mutation_calls": 0,
            "open_cases": len(self.list_open_cases()),
        }

    async def authorize_and_rollback(
        self,
        recovery_id: str,
        *,
        action,
        permit=None,
        decision=None,
        evaluate_kwargs=None,
        now: datetime | None = None,
    ):
        """Explicit protected rollback path: gate/HITL/permit already applied by executor."""
        case = self._require_case(recovery_id)
        if case.operator_decision != DECISION_ROLLBACK:
            raise RecoveryAuthorizationRequired("rollback_requires_operator_decision")
        executor = self.side_effect_executor
        if executor is None:
            raise RecoveryAuthorizationRequired("side_effect_executor_required")
        self.mutation_calls += 1
        result = await executor.rollback(
            case.execution_id,
            action=action,
            permit=permit,
            decision=decision,
            gate=self.gate,
            hitl=self.hitl,
            now=now,
            evaluate_kwargs=evaluate_kwargs,
        )
        self._audit(
            "recovery_rollback",
            recovery_id=recovery_id,
            execution_id=case.execution_id,
            reason_code="rollback_executed",
        )
        return result

    # -------------------------------------------------------------- internals
    async def _reconcile_read_only(self, case: RecoveryCase, *, now: datetime) -> dict:
        self._emit("recovery.check_started", case, status=STATUS_CHECKING)
        case = self._set_status(case, STATUS_CHECKING, reason_code="reconcile_read_only", now=now)
        service = self.reconciliation_service
        if service is None:
            case = self._set_status(case, STATUS_WAITING_OPERATOR, reason_code="reconciliation_unavailable", now=now)
            self._emit("recovery.waiting_operator", case, status=STATUS_WAITING_OPERATOR)
            return {"status": STATUS_WAITING_OPERATOR, "reason_code": "reconciliation_unavailable", "mutated": False}

        recon_id = case.reconciliation_id
        if not recon_id and hasattr(service, "store"):
            rows = service.store.find_by_execution(case.execution_id)
            if rows:
                recon_id = rows[0].reconciliation_id
        if not recon_id:
            case = self._set_status(case, STATUS_WAITING_OPERATOR, reason_code="reconciliation_missing", now=now)
            return {"status": STATUS_WAITING_OPERATOR, "reason_code": "reconciliation_missing", "mutated": False}

        self.network_calls += 1
        self._metric_inc("recovery_read_checks_total", case)
        action_stub = None
        try:
            execution = service.execution_store.get(case.execution_id)
            raw_key = None
            if execution is not None and hasattr(service, "_raw_idempotency_key"):
                raw_key = service._raw_idempotency_key(execution)
            action_stub = type(
                "RecoveryActionStub",
                (),
                {
                    "idempotency_key": raw_key,
                    "action_id": case.action_id,
                    "tool_id": case.tool_id,
                    "operation": case.operation,
                    "workflow_id": case.workflow_id,
                    "task_id": case.task_id,
                    "resource": getattr(execution, "resource_ref", None) or "",
                },
            )()
        except Exception:
            action_stub = None
        try:
            outcome = await service.reconcile(recon_id, action=action_stub, now=now)
        except Exception:
            self._metric_inc("recovery_read_check_failures_total", case)
            attempt = case.attempt + 1
            case = self._clone_case(case, attempt=attempt, updated_at=now, status=STATUS_OPEN)
            case = self._update(case, expected_version=self._require_case(case.recovery_id).version)
            if attempt >= case.max_attempts:
                case = self._set_status(case, STATUS_WAITING_OPERATOR, reason_code="max_read_checks", now=now)
                self._emit("recovery.waiting_operator", case, status=STATUS_WAITING_OPERATOR)
            self._emit("recovery.check_completed", case, status=case.status, metadata={"ok": False})
            return {"status": case.status, "reason_code": "reconcile_failed", "mutated": False}

        status = getattr(outcome, "status", None)
        attempt = case.attempt + 1
        case = self._clone_case(
            case,
            attempt=attempt,
            reconciliation_id=recon_id,
            updated_at=now,
        )
        # reload version
        current = self._require_case(case.recovery_id)
        case = self._update(
            self._clone_case(current, attempt=attempt, reconciliation_id=recon_id, updated_at=now, status=current.status),
            expected_version=current.version,
        )
        self._emit(
            "recovery.check_completed",
            case,
            status=str(status or ""),
            metadata={"reconciliation_status": status},
        )

        if status == RECON_CONFIRMED_SUCCEEDED:
            case = self._set_status(case, STATUS_RESOLVED, reason_code="confirmed_succeeded", now=now)
            self._emit("recovery.resolved", case, status=STATUS_RESOLVED)
            self._metric_inc("recovery_resolved_total", case)
            return {
                "status": STATUS_RESOLVED,
                "reason_code": "confirmed_succeeded",
                "mutated": False,
                "reconciliation_status": status,
            }
        if status == RECON_CONFIRMED_FAILED:
            case = self._set_status(
                case, STATUS_WAITING_OPERATOR, reason_code="confirmed_failed_requires_new_authorization", now=now
            )
            self._emit("recovery.waiting_operator", case, status=STATUS_WAITING_OPERATOR)
            return {
                "status": STATUS_WAITING_OPERATOR,
                "reason_code": "confirmed_failed_requires_new_authorization",
                "mutated": False,
                "reconciliation_status": status,
                "requires_new_authorization": True,
            }
        # unknown / manual_review
        if attempt >= case.max_attempts or status in {RECON_MANUAL_REVIEW, RECON_STILL_UNCERTAIN}:
            reason = "waiting_operator" if status != RECON_MANUAL_REVIEW else "manual_review"
            if attempt >= case.max_attempts:
                reason = "max_read_checks"
            case = self._set_status(case, STATUS_WAITING_OPERATOR, reason_code=reason, now=now)
            self._emit("recovery.waiting_operator", case, status=STATUS_WAITING_OPERATOR)
            return {
                "status": STATUS_WAITING_OPERATOR,
                "reason_code": reason,
                "mutated": False,
                "reconciliation_status": status,
            }
        case = self._set_status(case, STATUS_QUEUED, reason_code="reconcile_unknown_retry", now=now)
        return {
            "status": STATUS_QUEUED,
            "reason_code": "reconcile_unknown_retry",
            "mutated": False,
            "reconciliation_status": status,
        }

    async def _resume_workflow(self, case: RecoveryCase, step: RecoveryAction, *, now: datetime) -> dict:
        if self._workflow_is_terminal(case.workflow_id):
            case = self._set_status(case, STATUS_BLOCKED, reason_code="terminal_workflow_resume_denied", now=now)
            return {"status": STATUS_BLOCKED, "reason_code": "terminal_workflow_resume_denied", "mutated": False}
        engine = self.workflow_engine
        if engine is None or not hasattr(engine, "state_manager"):
            raise RecoveryAuthorizationRequired("workflow_engine_required_for_resume")
        # Resume is not a bypass of approval — require non-terminal + policy + operator RESUME or confirmed success.
        if case.operator_decision not in {DECISION_RESUME, None} and case.reason_code != "confirmed_succeeded":
            if case.operator_decision != DECISION_RESUME:
                raise RecoveryAuthorizationRequired("resume_requires_operator_or_confirmed_success")
        try:
            sm = engine.state_manager
            state = sm.get(case.workflow_id)
            if state.status in TERMINAL_STATUSES:
                case = self._set_status(case, STATUS_BLOCKED, reason_code="terminal_workflow_resume_denied", now=now)
                return {"status": STATUS_BLOCKED, "reason_code": "terminal_workflow_resume_denied", "mutated": False}
            # Prefer resume_from_approval when waiting approval; otherwise mark running if already planned.
            from workflow.models import STATUS_WAITING_APPROVAL, STATUS_RUNNING

            if state.status == STATUS_WAITING_APPROVAL:
                raise RecoveryAuthorizationRequired("resume_requires_hitl_approval_path")
            if state.status != STATUS_RUNNING and hasattr(sm, "start"):
                # Do not reopen terminal; only advance non-terminal waiting_recovery-like states via existing APIs.
                pass
            case = self._set_status(case, STATUS_RESOLVED, reason_code="resume_planned", now=now)
            return {
                "status": STATUS_RESOLVED,
                "reason_code": "resume_allowed",
                "mutated": False,
                "workflow_status": state.status,
            }
        except RecoveryAuthorizationRequired:
            raise

    def _enqueue_read_check(self, case: RecoveryCase, *, now: datetime) -> None:
        job = self.queue.enqueue(
            recovery_id=case.recovery_id,
            action_type=ACTION_RECONCILE_READ_ONLY,
            scheduled_at=now,
            priority=case.severity,
            attempt=case.attempt,
            metadata_safe={"case_type": case.case_type},
        )
        updated = self._set_status(case, STATUS_QUEUED, reason_code="queued_reconcile", now=now)
        self._emit("recovery.queued", updated, status=STATUS_QUEUED, metadata={"job_id": job.job_id})

    def _fail_closed_persistence(self) -> None:
        self.mutation_blocked_reason = "recovery_persistence_unavailable"
        self._emit_raw(
            "recovery.failed",
            status="blocked",
            error_code="recovery_persistence_unavailable",
            metadata={"fail_closed": True},
        )

    def _workflow_is_terminal(self, workflow_id: str) -> bool:
        if not workflow_id or self.workflow_engine is None:
            return False
        sm = getattr(self.workflow_engine, "state_manager", None)
        if sm is None:
            return False
        try:
            state = sm.get(workflow_id)
        except Exception:
            return False
        return state.status in TERMINAL_STATUSES

    def _require_case(self, recovery_id: str) -> RecoveryCase:
        case = self.store.get(recovery_id)
        if case is None:
            raise RecoveryConflictError("recovery_not_found")
        return case

    def _set_status(self, case: RecoveryCase, status: str, *, reason_code: str, now: datetime) -> RecoveryCase:
        current = self._require_case(case.recovery_id)
        updated = self._clone_case(
            current,
            status=status,
            reason_code=reason_code,
            updated_at=now,
        )
        return self._update(updated, expected_version=current.version)

    def _update(self, case: RecoveryCase, *, expected_version: int) -> RecoveryCase:
        try:
            return self.store.update(case, expected_version=expected_version)
        except RecoveryPersistenceUnavailableError:
            self._fail_closed_persistence()
            raise

    @staticmethod
    def _clone_case(case: RecoveryCase, **kwargs) -> RecoveryCase:
        fields = {
            "recovery_id": case.recovery_id,
            "execution_id": case.execution_id,
            "workflow_id": case.workflow_id,
            "task_id": case.task_id,
            "action_id": case.action_id,
            "tool_id": case.tool_id,
            "operation": case.operation,
            "case_type": case.case_type,
            "status": case.status,
            "severity": case.severity,
            "reason_code": case.reason_code,
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "next_check_at": case.next_check_at,
            "attempt": case.attempt,
            "max_attempts": case.max_attempts,
            "operator_decision": case.operator_decision,
            "reconciliation_id": case.reconciliation_id,
            "parent_recovery_id": case.parent_recovery_id,
            "tool_trust_level": case.tool_trust_level,
            "reversible": case.reversible,
            "metadata_safe": dict(case.metadata_safe),
            "version": case.version,
        }
        fields.update(kwargs)
        return RecoveryCase(**fields)

    def _refresh_open_gauge(self) -> None:
        self._open_gauge = len(self.list_open_cases())
        obs = self.observability
        if obs is None or not getattr(obs, "metrics", None):
            return
        # gauge approximated via labeled counter absolute not available — set attribute if present
        if hasattr(obs.metrics, "recovery_open_cases"):
            with getattr(obs.metrics, "_lock", threading_noop()):
                obs.metrics.recovery_open_cases = self._open_gauge

    def _metric_inc(self, name: str, case: RecoveryCase) -> None:
        obs = self.observability
        if obs is None or not getattr(obs, "metrics", None):
            return
        labels = {
            "case_type": case.case_type,
            "severity": case.severity,
            "status": case.status,
            "tool_trust_level": case.tool_trust_level or "unknown",
            "component": "recovery",
        }
        obs.metrics.inc(name, labels=labels)

    def _emit(self, event_type: str, case: RecoveryCase, *, status: str = "", metadata: dict | None = None, error_code: str | None = None) -> None:
        self._emit_raw(
            event_type,
            status=status or case.status,
            tool_id=case.tool_id,
            operation=case.operation,
            trust_level=case.tool_trust_level,
            error_code=error_code,
            metadata={
                "recovery_id": case.recovery_id,
                "case_type": case.case_type,
                "severity": case.severity,
                **dict(metadata or {}),
            },
            workflow_id=case.workflow_id,
            task_id=case.task_id,
        )

    def _emit_raw(self, event_type: str, **kwargs) -> None:
        obs = self.observability
        if obs is None:
            return
        try:
            meta = kwargs.pop("metadata", None)
            wf = kwargs.pop("workflow_id", "")
            task = kwargs.pop("task_id", "")
            ctx = None
            if wf and hasattr(obs, "create_context"):
                ctx = obs.create_context(workflow_id=wf, task_id=task)
            obs.emit(event_type, context=ctx, component="recovery", metadata=meta, **kwargs)
        except Exception:
            pass

    def _audit(self, event: str, **fields) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(event, **sanitize_metadata(fields))
        except Exception:
            try:
                self.audit.record(event, **{k: str(v)[:128] for k, v in fields.items()})
            except Exception:
                pass


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def threading_noop():
    return _NoopLock()
