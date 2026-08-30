import asyncio
import uuid
from dataclasses import replace

from autonomy.capabilities import CAP_FINANCIAL_CHANGE, CAP_PRICING_WRITE, CAP_PURCHASE
from autonomy.errors import IdempotencyConflictError
from autonomy.gate import AutonomyGate
from autonomy.idempotency import IdempotencyRegistry
from autonomy.models import (
    ACTION_WRITE,
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_FAILED,
    IDEMPOTENCY_RESERVED,
    IDEMPOTENCY_STARTED,
    IDEMPOTENCY_UNCERTAIN,
    SIDE_EFFECT_TYPES,
)
from hitl.errors import (
    ActionIntegrityError,
    ExecutionPermitConsumedError,
    ExecutionPermitExpiredError,
    ExecutionPermitMismatchError,
    ExecutionPermitRevokedError,
)
from hitl.models import action_fingerprint
from hitl.permit import PermitService
from workflow.models import (
    STATUS_RUNNING,
    STATUS_VALIDATING,
    STATUS_WAITING_APPROVAL,
    TERMINAL_STATUSES,
)

from side_effects.activation import DryRunResult, PURPOSE_DRY_RUN, PURPOSE_MUTATE, PURPOSE_ROLLBACK
from side_effects.audit import SideEffectAuditLog
from side_effects.errors import (
    RollbackExecutionError,
    RollbackNotSupportedError,
    SideEffectActivationDeniedError,
    SideEffectAdapterMismatchError,
    SideEffectAdapterNotFoundError,
    SideEffectAlreadyCompletedError,
    SideEffectAuthorizationError,
    SideEffectExecutionDeniedError,
    SideEffectExecutionError,
    SideEffectIdempotencyError,
    SideEffectPersistenceUnavailableError,
)
from side_effects.models import (
    AUTHORIZATION_AUTONOMY_DECISION,
    AUTHORIZATION_EXECUTION_PERMIT,
    DISABLED_ACTION_REASONS,
    EVENT_ADAPTER_FAILED,
    EVENT_ADAPTER_STARTED,
    EVENT_ADAPTER_SUCCEEDED,
    EVENT_DRY_RUN_COMPLETED,
    EVENT_DRY_RUN_REQUESTED,
    EVENT_EXECUTION_AUTHORIZED,
    EVENT_EXECUTION_COMPLETED,
    EVENT_EXECUTION_DENIED,
    EVENT_EXECUTION_REQUESTED,
    EVENT_EXECUTION_UNCERTAIN,
    EVENT_IDEMPOTENCY_RESERVED,
    EVENT_PERMIT_CONSUMED,
    EVENT_REAL_EXECUTION_AUTHORIZED,
    EVENT_ROLLBACK_FAILED,
    EVENT_ROLLBACK_REQUESTED,
    EVENT_ROLLBACK_SUCCEEDED,
    OUTCOME_KNOWN_FAILURE,
    OUTCOME_KNOWN_SUCCESS,
    OUTCOME_UNCERTAIN,
    P6A_EXECUTABLE_ACTION_TYPES,
    ROLLBACK_FAILED,
    ROLLBACK_NONE,
    ROLLBACK_SUCCEEDED,
    STATUS_CANCELLED,
    STATUS_DENIED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    STATUS_STARTED,
    SideEffectExecutionContext,
    SideEffectExecutionRecord,
    SideEffectExecutionRequest,
    SideEffectExecutionResult,
    hash_idempotency_key,
)
from side_effects.registry import SideEffectAdapterRegistry, empty_adapter_registry
from side_effects.store import InMemorySideEffectExecutionStore


# Best-effort single-process duplicate prevention. Not distributed exactly-once.
EXACTLY_ONCE_CLAIM = False


class SideEffectExecutor:
    """Sole owner of actual side-effect invocation. Adapters are never called directly."""

    def __init__(
        self,
        registry: SideEffectAdapterRegistry | None = None,
        *,
        store=None,
        audit: SideEffectAuditLog | None = None,
        idempotency: IdempotencyRegistry | None = None,
        gate: AutonomyGate | None = None,
        permit_service: PermitService | None = None,
        reconciliation_service=None,
        activation=None,
        persistence=None,
        require_durable_persistence: bool = False,
    ):
        self.registry = registry if registry is not None else empty_adapter_registry()
        self.store = store or InMemorySideEffectExecutionStore()
        self.audit = audit or SideEffectAuditLog()
        self.idempotency = idempotency
        self.gate = gate
        self.permit_service = permit_service or PermitService()
        self.reconciliation_service = reconciliation_service
        self.activation = activation
        self.persistence = persistence
        self.require_durable_persistence = bool(require_durable_persistence)
        self.trace: list[str] = []
        self.simulate_finalization_failure = False
        self.observability = None
        self.recovery_orchestrator = None

    def _obs_emit(self, event_type: str, action, **kwargs):
        from observability.helpers import safe_emit

        if self.observability is None:
            return
        parent = self.observability.context_for_workflow(action.workflow_id)
        span = (
            self.observability.child_span(parent)
            if parent is not None
            else self.observability.create_context(
                workflow_id=action.workflow_id, task_id=action.task_id
            )
        )
        safe_emit(
            self.observability,
            event_type,
            context=span,
            component="side_effect",
            tool_id=action.tool_id,
            operation=action.operation,
            trust_level=action.tool_trust_level,
            **kwargs,
        )

    async def execute(
        self,
        action,
        *,
        decision=None,
        permit=None,
        context: SideEffectExecutionContext | None = None,
        gate: AutonomyGate | None = None,
        hitl=None,
        state_manager=None,
        now=None,
        timeout_seconds: float | None = None,
        evaluate_kwargs=None,
    ) -> SideEffectExecutionResult:
        ctx = context or SideEffectExecutionContext()
        stamp = now or ctx.stamp()
        ctx.now = stamp
        evaluate_kwargs = dict(evaluate_kwargs or {})
        gate = gate or self.gate or AutonomyGate()
        idempotency = self.idempotency or gate.idempotency
        self.idempotency = idempotency
        execution_id = str(uuid.uuid4())
        self.trace = []
        adapter_started = False
        permit_consumed = False
        adapter_result = None
        authorization_type = None
        authorization_id = ""
        started_at = stamp
        tenant_id = ""

        self.audit.record(
            EVENT_EXECUTION_REQUESTED,
            execution_id=execution_id,
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            action_id=action.action_id,
            tool_id=action.tool_id,
            operation=action.operation,
        )
        self._obs_emit(
            "side_effect.prepared",
            action,
            status="prepared",
            metadata={"execution_id": execution_id},
            update_metrics=False,
        )
        try:
            # Soft resolve early so pre-mutate denials persist with ownership when known.
            # Fail-closed tenant require runs only before mutate/start persistence.
            tenant_id = self._peek_execution_tenant(action, state_manager)
            self._deny_disabled_actions(action)
            self._require_executable_action_type(action)
            if not action.idempotency_key:
                raise SideEffectIdempotencyError("idempotency_required")
            completed = self._existing_completed(action)
            if completed is not None:
                return self._result_from_record(completed)
            self._require_workflow_running(state_manager, action.workflow_id)
            tenant_id = self._require_execution_tenant(action, state_manager)
            fingerprint = action_fingerprint(action)
            authorization_type, authorization_id = self._authorize(
                action,
                decision=decision,
                permit=permit,
                gate=gate,
                hitl=hitl,
                fingerprint=fingerprint,
                now=stamp,
                evaluate_kwargs=evaluate_kwargs,
            )
            adapter = self.registry.require(action.tool_id)
            self._validate_adapter(action, adapter, fingerprint)
            self._require_activation(action, adapter, purpose=PURPOSE_MUTATE, now=stamp)
            self._require_persistence_ready(purpose=PURPOSE_MUTATE)
            self._require_recovery_persistence_ready(purpose=PURPOSE_MUTATE)
            self._require_protected_state_ready(
                authorization_type=authorization_type, purpose=PURPOSE_MUTATE
            )
            if self.activation is not None:
                self.audit.record(
                    EVENT_REAL_EXECUTION_AUTHORIZED,
                    execution_id=execution_id,
                    workflow_id=action.workflow_id,
                    action_id=action.action_id,
                    tool_id=action.tool_id,
                    reason_code="activation_ready",
                )
            try:
                self._reserve_or_start(action, execution_id)
                self._persist_started_and_consume_permit(
                    action=action,
                    execution_id=execution_id,
                    authorization_type=authorization_type,
                    authorization_id=authorization_id,
                    started_at=started_at,
                    permit=permit if authorization_type == AUTHORIZATION_EXECUTION_PERMIT else None,
                    hitl=hitl,
                    stamp=stamp,
                    tenant_id=tenant_id,
                )
                if authorization_type == AUTHORIZATION_EXECUTION_PERMIT:
                    permit_consumed = True
                    self.trace.append("permit_consumed")
                    self.audit.record(
                        EVENT_PERMIT_CONSUMED,
                        execution_id=execution_id,
                        workflow_id=action.workflow_id,
                        action_id=action.action_id,
                        authorization_type=authorization_type,
                        authorization_id=authorization_id,
                    )
            except SideEffectPersistenceUnavailableError:
                raise
            except Exception as exc:
                if isinstance(exc, (SideEffectAlreadyCompletedError, SideEffectIdempotencyError)):
                    raise
                if isinstance(
                    exc,
                    (
                        ExecutionPermitExpiredError,
                        ExecutionPermitConsumedError,
                        ExecutionPermitRevokedError,
                        ExecutionPermitMismatchError,
                        SideEffectAuthorizationError,
                        ActionIntegrityError,
                    ),
                ):
                    raise
                raise SideEffectPersistenceUnavailableError() from exc
            self.audit.record(
                EVENT_IDEMPOTENCY_RESERVED,
                execution_id=execution_id,
                workflow_id=action.workflow_id,
                action_id=action.action_id,
                metadata={"idempotency_key_hash": hash_idempotency_key(action.idempotency_key)},
            )
            request = SideEffectExecutionRequest(
                execution_id=execution_id,
                workflow_id=action.workflow_id,
                task_id=action.task_id,
                action_id=action.action_id,
                tool_id=action.tool_id,
                operation=action.operation,
                resource=action.resource,
                action_fingerprint=fingerprint,
                idempotency_key=action.idempotency_key,
                authorization_type=authorization_type,
                authorization_id=authorization_id,
                requested_at=stamp,
                metadata={"attempt": 1},
            )
            _ = request
            adapter_started = True
            self.trace.append("adapter_started")
            self.audit.record(
                EVENT_ADAPTER_STARTED,
                execution_id=execution_id,
                workflow_id=action.workflow_id,
                action_id=action.action_id,
                tool_id=action.tool_id,
                operation=action.operation,
            )
            self._obs_emit(
                "side_effect.started",
                action,
                status="started",
                metadata={"execution_id": execution_id},
            )
            ctx.idempotency_key = action.idempotency_key
            ctx.tenant_id = tenant_id
            timeout = timeout_seconds if timeout_seconds is not None else ctx.timeout_seconds
            # Cancellation fence immediately before protected mutate (STOP FUTURE MUTATION).
            self._require_workflow_running(state_manager, action.workflow_id)
            adapter_result = await self._invoke_adapter(adapter, action, ctx, timeout)
            if ctx.simulate_finalization_failure or self.simulate_finalization_failure:
                raise SideEffectExecutionError("finalization_failed")
            completed_at = ctx.stamp()
            result = SideEffectExecutionResult(
                execution_id=execution_id,
                workflow_id=action.workflow_id,
                task_id=action.task_id,
                action_id=action.action_id,
                tool_id=action.tool_id,
                operation=action.operation,
                status=STATUS_SUCCEEDED,
                started_at=started_at,
                completed_at=completed_at,
                outcome=OUTCOME_KNOWN_SUCCESS,
                external_reference=adapter_result.external_reference,
                reversible=bool(adapter_result.reversible),
                rollback_reference=adapter_result.rollback_reference,
                metadata={
                    "duration_ms": int((completed_at - started_at).total_seconds() * 1000),
                    "authorization_type": authorization_type,
                    "attempt": 1,
                    **dict(adapter_result.metadata),
                },
            )
            try:
                self._finalize_success(
                    result,
                    authorization_type,
                    authorization_id,
                    action,
                    attempt=1,
                    tenant_id=tenant_id,
                )
            except Exception as exc:
                # External success already confirmed; local durability failure → uncertain.
                error_code = "execution_outcome_uncertain"
                try:
                    self.idempotency.mark_uncertain(action.idempotency_key)
                except Exception:
                    pass
                uncertain_result = SideEffectExecutionResult(
                    execution_id=execution_id,
                    workflow_id=action.workflow_id,
                    task_id=action.task_id,
                    action_id=action.action_id,
                    tool_id=action.tool_id,
                    operation=action.operation,
                    status=STATUS_UNKNOWN,
                    started_at=started_at,
                    completed_at=ctx.stamp(),
                    outcome=OUTCOME_UNCERTAIN,
                    error_code=error_code,
                    external_reference=adapter_result.external_reference,
                    reversible=bool(adapter_result.reversible),
                    rollback_reference=adapter_result.rollback_reference,
                    metadata={
                        "finalization_error": getattr(exc, "error_code", type(exc).__name__),
                        "adapter_started": True,
                    },
                )
                try:
                    self._save_record(
                        uncertain_result,
                        authorization_type,
                        authorization_id,
                        action,
                        attempt=1,
                        tenant_id=tenant_id,
                    )
                except Exception:
                    pass
                self._flag_reconciliation(uncertain_result)
                self.audit.record(
                    EVENT_EXECUTION_UNCERTAIN,
                    execution_id=execution_id,
                    workflow_id=action.workflow_id,
                    action_id=action.action_id,
                    reason_code=error_code,
                )
                self._obs_emit(
                    "side_effect.uncertain",
                    action,
                    status="uncertain",
                    error_code=error_code,
                    metadata={
                        "execution_id": execution_id,
                        "outcome": "uncertain",
                        "reconciliation_required": True,
                    },
                )
                return uncertain_result
            self.audit.record(
                EVENT_ADAPTER_SUCCEEDED,
                execution_id=execution_id,
                workflow_id=action.workflow_id,
                action_id=action.action_id,
                tool_id=action.tool_id,
                metadata={"external_reference": adapter_result.external_reference},
            )
            self.audit.record(
                EVENT_EXECUTION_COMPLETED,
                execution_id=execution_id,
                workflow_id=action.workflow_id,
                action_id=action.action_id,
                reason_code="succeeded",
            )
            self._obs_emit(
                "side_effect.completed",
                action,
                status="succeeded",
                duration_ms=int((completed_at - started_at).total_seconds() * 1000),
                metadata={
                    "execution_id": execution_id,
                    "outcome": "known_success",
                },
            )
            return result
        except SideEffectAlreadyCompletedError:
            raise
        except Exception as exc:
            error_code = getattr(exc, "error_code", None) or type(exc).__name__
            if isinstance(exc, asyncio.TimeoutError):
                error_code = "execution_timeout"
            mutated = False
            adapter = self.registry.get(action.tool_id)
            if adapter is not None:
                mutated = bool(getattr(adapter, "mutated", False))
            uncertain = adapter_started and (
                error_code
                in {
                    "finalization_failed",
                    "adapter_failed_after_write",
                    "external_write_timeout_uncertain",
                    "external_verification_uncertain",
                }
                or (isinstance(exc, asyncio.TimeoutError) and mutated)
            )
            if uncertain:
                outcome = OUTCOME_UNCERTAIN
                status = STATUS_UNKNOWN
                error_code = "execution_outcome_uncertain"
                try:
                    self.idempotency.mark_uncertain(action.idempotency_key)
                except Exception:
                    pass
                self.audit.record(
                    EVENT_EXECUTION_UNCERTAIN,
                    execution_id=execution_id,
                    workflow_id=action.workflow_id,
                    action_id=action.action_id,
                    reason_code=error_code,
                )
                self._obs_emit(
                    "side_effect.uncertain",
                    action,
                    status="uncertain",
                    error_code=error_code,
                    exception_type=type(exc).__name__,
                    metadata={
                        "execution_id": execution_id,
                        "outcome": "uncertain",
                        "reconciliation_required": True,
                    },
                )
                self._fail_workflow(state_manager, action.workflow_id, error_code)
            elif adapter_started:
                outcome = OUTCOME_KNOWN_FAILURE
                status = STATUS_FAILED if error_code != "execution_timeout" else STATUS_CANCELLED
                try:
                    self.idempotency.mark_failed(action.idempotency_key)
                except Exception:
                    pass
                self.audit.record(
                    EVENT_ADAPTER_FAILED,
                    execution_id=execution_id,
                    workflow_id=action.workflow_id,
                    action_id=action.action_id,
                    reason_code=str(error_code),
                )
                self._obs_emit(
                    "side_effect.failed",
                    action,
                    status="failed",
                    error_code=str(error_code),
                    exception_type=type(exc).__name__,
                    metadata={"execution_id": execution_id, "outcome": "known_failure"},
                )
                self._fail_workflow(state_manager, action.workflow_id, str(error_code))
            else:
                outcome = OUTCOME_KNOWN_FAILURE
                status = STATUS_DENIED
                self.audit.record(
                    EVENT_EXECUTION_DENIED,
                    execution_id=execution_id,
                    workflow_id=action.workflow_id,
                    action_id=action.action_id,
                    reason_code=str(error_code),
                )
            completed_at = ctx.stamp()
            extra_meta = {}
            ext_ref = None
            rb_ref = None
            if adapter_result is not None:
                extra_meta = dict(adapter_result.metadata)
                ext_ref = adapter_result.external_reference
                rb_ref = adapter_result.rollback_reference
            result = SideEffectExecutionResult(
                execution_id=execution_id,
                workflow_id=action.workflow_id,
                task_id=action.task_id,
                action_id=action.action_id,
                tool_id=action.tool_id,
                operation=action.operation,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                outcome=outcome,
                error_code=str(error_code),
                external_reference=ext_ref,
                rollback_reference=rb_ref,
                reversible=bool(adapter_result.reversible) if adapter_result is not None else False,
                metadata={
                    "permit_consumed": permit_consumed,
                    "adapter_started": adapter_started,
                    **extra_meta,
                },
            )
            try:
                self._save_record(
                    result,
                    authorization_type or AUTHORIZATION_AUTONOMY_DECISION,
                    authorization_id,
                    action,
                    attempt=1,
                    tenant_id=tenant_id,
                )
            except SideEffectExecutionDeniedError as save_exc:
                # Never mask the original pre-mutate denial/error with tenant fail-closed
                # from denial-record persistence when tenant was not yet required.
                if (
                    not adapter_started
                    and getattr(save_exc, "error_code", None)
                    == "side_effect_tenant_required"
                ):
                    pass
                else:
                    raise
            if uncertain:
                self._flag_reconciliation(result)
                return result
            if adapter_started:
                raise SideEffectExecutionError(str(error_code)) from exc
            if isinstance(
                exc,
                (
                    SideEffectExecutionDeniedError,
                    SideEffectAuthorizationError,
                    SideEffectIdempotencyError,
                    SideEffectAdapterNotFoundError,
                    SideEffectAdapterMismatchError,
                    ActionIntegrityError,
                    SideEffectActivationDeniedError,
                    SideEffectPersistenceUnavailableError,
                ),
            ):
                raise
            raise SideEffectExecutionDeniedError(str(error_code)) from exc

    async def rollback(
        self,
        execution_id: str,
        *,
        action,
        decision=None,
        permit=None,
        context: SideEffectExecutionContext | None = None,
        gate: AutonomyGate | None = None,
        hitl=None,
        now=None,
        evaluate_kwargs=None,
    ) -> SideEffectExecutionResult:
        ctx = context or SideEffectExecutionContext()
        stamp = now or ctx.stamp()
        original = self.store.get(execution_id)
        if original is None:
            raise SideEffectExecutionDeniedError("execution_not_found")
        if original.status != STATUS_SUCCEEDED:
            raise SideEffectExecutionDeniedError("rollback_requires_success")
        if original.rollback_status == ROLLBACK_SUCCEEDED:
            return self._result_from_record(original)
        adapter = self.registry.require(original.tool_id)
        if not getattr(adapter, "reversible", False):
            raise RollbackNotSupportedError()
        if not original.rollback_reference:
            raise RollbackNotSupportedError("rollback_reference_missing")
        if not action.idempotency_key:
            raise SideEffectIdempotencyError("idempotency_required")
        self.audit.record(
            EVENT_ROLLBACK_REQUESTED,
            execution_id=execution_id,
            workflow_id=action.workflow_id,
            action_id=action.action_id,
            tool_id=action.tool_id,
        )
        gate = gate or self.gate or AutonomyGate()
        if self.idempotency is None:
            self.idempotency = gate.idempotency
        fingerprint = action_fingerprint(action)
        self._authorize(
            action,
            decision=decision,
            permit=permit,
            gate=gate,
            hitl=hitl,
            fingerprint=fingerprint,
            now=stamp,
            evaluate_kwargs=dict(evaluate_kwargs or {}),
        )
        existing = self.idempotency.get(action.idempotency_key)
        if existing is not None and existing.state == IDEMPOTENCY_COMPLETED:
            return self._result_from_record(original)
        self._require_activation(action, adapter, purpose=PURPOSE_ROLLBACK, now=stamp)
        self._require_persistence_ready(purpose=PURPOSE_ROLLBACK)
        self._require_recovery_persistence_ready(purpose=PURPOSE_ROLLBACK)
        try:
            self._reserve_or_start(action, execution_id + ":rollback")
        except SideEffectAlreadyCompletedError:
            return self._result_from_record(original)
        if permit is not None:
            self._consume_permit(permit, action, hitl, stamp)
        ctx.resource = action.resource
        try:
            await adapter.rollback(
                {
                    "rollback_reference": original.rollback_reference,
                    "external_reference": original.external_reference,
                },
                ctx,
            )
        except RollbackNotSupportedError:
            raise
        except Exception as exc:
            updated = replace(
                original,
                rollback_status=ROLLBACK_FAILED,
                version=int(original.version) + 1,
            )
            self.store.save(updated)
            try:
                self.idempotency.mark_failed(action.idempotency_key)
            except Exception:
                pass
            self.audit.record(
                EVENT_ROLLBACK_FAILED,
                execution_id=execution_id,
                workflow_id=action.workflow_id,
                action_id=action.action_id,
                reason_code=getattr(exc, "error_code", "rollback_failed"),
            )
            raise RollbackExecutionError() from exc
        updated = replace(
            original,
            rollback_status=ROLLBACK_SUCCEEDED,
            version=int(original.version) + 1,
        )
        self.store.save(updated)
        self.idempotency.mark_completed(action.idempotency_key)
        self.audit.record(
            EVENT_ROLLBACK_SUCCEEDED,
            execution_id=execution_id,
            workflow_id=action.workflow_id,
            action_id=action.action_id,
        )
        return self._result_from_record(updated)

    def _authorize(
        self,
        action,
        *,
        decision,
        permit,
        gate,
        hitl,
        fingerprint,
        now,
        evaluate_kwargs,
    ) -> tuple[str, str]:
        if permit is None and decision is None:
            raise SideEffectAuthorizationError("authorization_required")
        if permit is not None:
            self._validate_permit(permit, action, hitl, now, fingerprint)
            self.audit.record(
                EVENT_EXECUTION_AUTHORIZED,
                workflow_id=action.workflow_id,
                action_id=action.action_id,
                authorization_type=AUTHORIZATION_EXECUTION_PERMIT,
                authorization_id=permit.permit_id,
            )
            return AUTHORIZATION_EXECUTION_PERMIT, permit.permit_id
        if decision.action_id != action.action_id:
            raise SideEffectAuthorizationError("decision_action_mismatch")
        if decision.decision == DECISION_DENY:
            raise SideEffectAuthorizationError("decision_deny")
        if decision.decision == DECISION_REQUIRE_APPROVAL:
            raise SideEffectAuthorizationError("execution_permit_required")
        if decision.decision != DECISION_ALLOW:
            raise SideEffectAuthorizationError("decision_not_allow")
        kwargs = dict(evaluate_kwargs)
        kwargs.pop("now", None)
        current = gate.evaluate(action, now=now, **kwargs)
        if current.decision != DECISION_ALLOW:
            raise SideEffectAuthorizationError("stale_allow_or_not_allow")
        if action_fingerprint(action) != fingerprint:
            raise ActionIntegrityError()
        self.audit.record(
            EVENT_EXECUTION_AUTHORIZED,
            workflow_id=action.workflow_id,
            action_id=action.action_id,
            authorization_type=AUTHORIZATION_AUTONOMY_DECISION,
            authorization_id=decision.decision_id,
        )
        return AUTHORIZATION_AUTONOMY_DECISION, decision.decision_id

    def _validate_permit(self, permit, action, hitl, now, fingerprint):
        service = hitl.permits if hitl is not None else self.permit_service
        try:
            service.validate(permit, action=action, now=now)
        except (
            ExecutionPermitExpiredError,
            ExecutionPermitConsumedError,
            ExecutionPermitRevokedError,
            ExecutionPermitMismatchError,
        ) as exc:
            raise SideEffectAuthorizationError(str(exc)) from exc
        if permit.action_fingerprint != fingerprint:
            raise ActionIntegrityError()
        if permit.tool_id != action.tool_id:
            raise SideEffectAuthorizationError("permit_tool_mismatch")
        if permit.operation != action.operation:
            raise SideEffectAuthorizationError("permit_operation_mismatch")
        if permit.idempotency_key != action.idempotency_key:
            raise SideEffectAuthorizationError("permit_idempotency_mismatch")

    def _consume_permit(self, permit, action, hitl, now):
        if hitl is not None:
            hitl.consume_for_execution(permit.permit_id, action=action, now=now)
            return
        self.permit_service.consume_for_execution(
            permit.permit_id, action=action, now=now
        )

    def _validate_adapter(self, action, adapter, fingerprint):
        descriptor = adapter.descriptor
        if action.operation not in descriptor.operations:
            raise SideEffectAdapterMismatchError("unknown_operation")
        resource = str(action.resource or "")
        if not resource.startswith(descriptor.resource_prefix):
            raise SideEffectAdapterMismatchError("resource_out_of_scope")
        if action.tool_trust_level != descriptor.trust_level:
            raise SideEffectAdapterMismatchError("trust_mismatch")
        action_reversible = bool(dict(action.metadata).get("reversible", False))
        if action_reversible != bool(descriptor.reversible):
            raise SideEffectAdapterMismatchError("reversible_mismatch")
        required = tuple(descriptor.capabilities_required)
        requested = set(action.requested_capabilities)
        if not set(required) <= requested:
            raise SideEffectAuthorizationError("capability_missing")
        if action_fingerprint(action) != fingerprint:
            raise ActionIntegrityError()

    def _require_activation(self, action, adapter, *, purpose: str, now) -> None:
        provider = self.activation
        if provider is None:
            return
        descriptor = getattr(adapter, "descriptor", None)
        decision = provider.evaluate(action, descriptor, purpose=purpose, now=now)
        if purpose in {PURPOSE_MUTATE, PURPOSE_ROLLBACK}:
            if decision.blocked or not decision.allowed or decision.dry_run:
                raise SideEffectActivationDeniedError(decision.reason_code)

    async def dry_run(
        self,
        action,
        *,
        context: SideEffectExecutionContext | None = None,
        gate: AutonomyGate | None = None,
        now=None,
        evaluate_kwargs=None,
    ) -> DryRunResult:
        ctx = context or SideEffectExecutionContext()
        stamp = now or ctx.stamp()
        ctx.now = stamp
        self.audit.record(
            EVENT_DRY_RUN_REQUESTED,
            workflow_id=action.workflow_id,
            action_id=action.action_id,
            tool_id=action.tool_id,
            operation=action.operation,
        )
        self._deny_disabled_actions(action)
        self._require_executable_action_type(action)
        adapter = self.registry.require(action.tool_id)
        fingerprint = action_fingerprint(action)
        self._validate_adapter(action, adapter, fingerprint)
        if self.activation is not None:
            decision = self.activation.evaluate(
                action, adapter.descriptor, purpose=PURPOSE_DRY_RUN, now=stamp
            )
            if decision.blocked:
                raise SideEffectActivationDeniedError(decision.reason_code)
        would_require_approval = False
        gate = gate or self.gate
        if gate is not None:
            kwargs = dict(evaluate_kwargs or {})
            kwargs.pop("now", None)
            preview_gate = AutonomyGate(
                policy=gate.policy,
                classifier=gate.classifier,
                autonomy_level=kwargs.get("autonomy_level") or gate.autonomy_level,
            )
            current = preview_gate.evaluate(action, now=stamp, **kwargs)
            would_require_approval = current.decision == DECISION_REQUIRE_APPROVAL
        if not hasattr(adapter, "dry_run"):
            raise SideEffectExecutionDeniedError("dry_run_unsupported")
        planned = await adapter.dry_run(action, ctx)
        result = DryRunResult(
            would_execute=planned.would_execute,
            would_change=planned.would_change,
            current_state_known=planned.current_state_known,
            intended_operation=planned.intended_operation,
            resource_ref=planned.resource_ref,
            reason_code=planned.reason_code,
            checked_at=planned.checked_at,
            would_require_approval=would_require_approval,
            metadata=dict(planned.metadata),
        )
        self.audit.record(
            EVENT_DRY_RUN_COMPLETED,
            workflow_id=action.workflow_id,
            action_id=action.action_id,
            tool_id=action.tool_id,
            operation=action.operation,
            reason_code=result.reason_code,
            metadata={
                "would_change": result.would_change,
                "would_execute": result.would_execute,
                "current_state_known": result.current_state_known,
            },
        )
        return result

    def _deny_disabled_actions(self, action):
        reason = DISABLED_ACTION_REASONS.get(action.action_type)
        if reason:
            raise SideEffectExecutionDeniedError(reason)
        requested = set(action.requested_capabilities)
        if CAP_PURCHASE in requested or CAP_FINANCIAL_CHANGE in requested:
            raise SideEffectExecutionDeniedError("financial_execution_not_enabled")
        if CAP_PRICING_WRITE in requested:
            raise SideEffectExecutionDeniedError("pricing_write_not_enabled")
        if action.action_type in SIDE_EFFECT_TYPES and action.action_type not in P6A_EXECUTABLE_ACTION_TYPES:
            raise SideEffectExecutionDeniedError("action_type_not_enabled")

    def _require_executable_action_type(self, action):
        if action.action_type != ACTION_WRITE:
            raise SideEffectExecutionDeniedError("action_type_not_enabled")

    def _peek_execution_tenant(self, action, state_manager) -> str:
        """Best-effort tenant from trusted workflow state; empty when unknown."""
        if state_manager is None:
            return ""
        try:
            state = state_manager.get(action.workflow_id)
            tid = getattr(state, "tenant_id", None)
            if tid:
                return str(tid).strip()
            meta = getattr(state, "metadata", None) or {}
            if isinstance(meta, dict):
                raw = str(meta.get("tenant_id") or "").strip()
                if raw:
                    return raw
        except Exception:
            return ""
        return ""

    def _require_execution_tenant(self, action, state_manager) -> str:
        """Resolve tenant from trusted workflow state; fail closed when missing."""
        from security.tenant import MissingTenantError, require_tenant_id

        tid = self._peek_execution_tenant(action, state_manager) or None
        try:
            return require_tenant_id(tid)
        except MissingTenantError as exc:
            raise SideEffectExecutionDeniedError("side_effect_tenant_required") from exc

    def _require_workflow_running(self, state_manager, workflow_id: str):
        if state_manager is None:
            return
        try:
            state = state_manager.get(workflow_id)
        except Exception:
            return
        if state.status == STATUS_WAITING_APPROVAL:
            raise SideEffectExecutionDeniedError("workflow_waiting_approval")
        if state.status in TERMINAL_STATUSES:
            raise SideEffectExecutionDeniedError("workflow_terminal")
        if state.status not in {STATUS_RUNNING, STATUS_VALIDATING}:
            raise SideEffectExecutionDeniedError("workflow_not_running")

    def _existing_completed(self, action):
        record = self.store.find_by_idempotency(hash_idempotency_key(action.idempotency_key))
        existing = self.idempotency.get(action.idempotency_key)
        if existing is None:
            return None
        if existing.state == IDEMPOTENCY_COMPLETED:
            if record is not None:
                return record
            raise SideEffectAlreadyCompletedError()
        if existing.state == IDEMPOTENCY_UNCERTAIN:
            raise SideEffectIdempotencyError("duplicate_uncertain")
        if existing.state == IDEMPOTENCY_FAILED:
            raise SideEffectIdempotencyError("duplicate_failed")
        if existing.state == IDEMPOTENCY_STARTED and existing.action_id != action.action_id:
            raise SideEffectIdempotencyError("duplicate_active")
        if existing.state == IDEMPOTENCY_RESERVED and existing.action_id != action.action_id:
            raise SideEffectIdempotencyError("duplicate_active")
        return None

    def _reserve_or_start(self, action, execution_id: str) -> None:
        existing = self.idempotency.get(action.idempotency_key)
        if existing is None:
            try:
                self.idempotency.reserve(action.idempotency_key, action.action_id)
            except IdempotencyConflictError as exc:
                raise SideEffectIdempotencyError(exc.reason_code) from exc
            self.idempotency.mark_started(action.idempotency_key)
            return
        if existing.state == IDEMPOTENCY_COMPLETED:
            raise SideEffectAlreadyCompletedError()
        if existing.state in {IDEMPOTENCY_UNCERTAIN}:
            raise SideEffectIdempotencyError("duplicate_uncertain")
        if existing.state == IDEMPOTENCY_FAILED:
            raise SideEffectIdempotencyError("duplicate_failed")
        if existing.action_id != action.action_id:
            raise SideEffectIdempotencyError("duplicate_active")
        if existing.state == IDEMPOTENCY_RESERVED:
            self.idempotency.mark_started(action.idempotency_key)
            return
        if existing.state == IDEMPOTENCY_STARTED:
            raise SideEffectIdempotencyError("duplicate_active")
        raise SideEffectIdempotencyError("duplicate_execution")

    async def _invoke_adapter(self, adapter, action, ctx, timeout):
        pending = adapter.execute(action, ctx)
        if timeout is None:
            return await pending
        try:
            return await asyncio.wait_for(pending, timeout=float(timeout))
        except asyncio.TimeoutError:
            raise

    def _flag_reconciliation(self, result) -> None:
        service = self.reconciliation_service
        recon_id = None
        if service is not None:
            try:
                record = service.create_for_execution(result.execution_id)
                recon_id = getattr(record, "reconciliation_id", None)
            except Exception:
                recon_id = None
        self._flag_recovery_case(result, reconciliation_id=recon_id)

    def _flag_recovery_case(self, result, *, reconciliation_id: str | None = None) -> None:
        orch = self.recovery_orchestrator
        if orch is None or not getattr(orch, "enabled", True):
            return
        try:
            orch.ensure_case_for_uncertain(
                execution_id=result.execution_id,
                workflow_id=getattr(result, "workflow_id", "") or "",
                task_id=getattr(result, "task_id", "") or "",
                action_id=getattr(result, "action_id", "") or "",
                tool_id=getattr(result, "tool_id", "") or "",
                operation=getattr(result, "operation", "") or "",
                tool_trust_level=str(
                    dict(getattr(result, "metadata", {}) or {}).get("tool_trust_level") or ""
                ),
                reversible=bool(getattr(result, "reversible", False)),
                reconciliation_id=reconciliation_id,
            )
        except Exception as exc:
            from recovery.store import RecoveryPersistenceUnavailableError

            if isinstance(exc, RecoveryPersistenceUnavailableError):
                # Fail closed for future related protected mutation.
                return
            return

    def _require_recovery_persistence_ready(self, *, purpose: str) -> None:
        if purpose not in {PURPOSE_MUTATE, PURPOSE_ROLLBACK}:
            return
        orch = self.recovery_orchestrator
        if orch is None:
            return
        from recovery.orchestrator import RecoveryMutationBlocked
        from recovery.store import RecoveryPersistenceUnavailableError
        from side_effects.errors import SideEffectPersistenceUnavailableError

        try:
            orch.require_mutation_allowed()
        except RecoveryMutationBlocked as exc:
            raise SideEffectPersistenceUnavailableError(str(exc.reason)) from exc
        store = getattr(orch, "store", None)
        if store is not None and getattr(store, "available", True) is False:
            raise SideEffectPersistenceUnavailableError("recovery_persistence_unavailable")

    def _require_persistence_ready(self, *, purpose: str) -> None:
        if purpose not in {PURPOSE_MUTATE, PURPOSE_ROLLBACK}:
            return
        if not self.require_durable_persistence:
            return
        bundle = self.persistence
        if bundle is None or not getattr(bundle, "ready", False):
            raise SideEffectPersistenceUnavailableError()

    def _require_protected_state_ready(
        self, *, authorization_type: str | None, purpose: str
    ) -> None:
        if purpose not in {PURPOSE_MUTATE, PURPOSE_ROLLBACK}:
            return
        if authorization_type != AUTHORIZATION_EXECUTION_PERMIT:
            return
        if not self.require_durable_persistence:
            return
        bundle = self.persistence
        if bundle is None or not getattr(bundle, "protected_state_ready", False):
            raise SideEffectPersistenceUnavailableError(
                "protected_state_persistence_unavailable"
            )

    def _persist_started_and_consume_permit(
        self,
        *,
        action,
        execution_id: str,
        authorization_type: str,
        authorization_id: str,
        started_at,
        permit=None,
        hitl=None,
        stamp=None,
        tenant_id: str = "",
    ) -> None:
        """Persist started execution, then durable-consume permit before any mutation."""

        def _body():
            self._persist_started_execution(
                action=action,
                execution_id=execution_id,
                authorization_type=authorization_type,
                authorization_id=authorization_id,
                started_at=started_at,
                permit=permit,
                use_uow=False,
                tenant_id=tenant_id,
            )
            if permit is not None:
                self._consume_permit(permit, action, hitl, stamp)

        uow = None
        if self.persistence is not None:
            uow = self.persistence.unit_of_work()
        if uow is not None:
            with uow:
                _body()
        else:
            _body()

    def _persist_started_execution(
        self,
        *,
        action,
        execution_id: str,
        authorization_type: str,
        authorization_id: str,
        started_at,
        permit=None,
        use_uow: bool = True,
        tenant_id: str = "",
    ) -> None:
        key = action.idempotency_key or ""
        permit_id = None
        approval_id = None
        if permit is not None:
            permit_id = permit.permit_id
            approval_id = permit.approval_id
        elif authorization_type == AUTHORIZATION_EXECUTION_PERMIT and authorization_id:
            permit_id = authorization_id
        record = SideEffectExecutionRecord(
            execution_id=execution_id,
            action_id=action.action_id,
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            tool_id=action.tool_id,
            operation=action.operation,
            status=STATUS_STARTED,
            authorization_type=authorization_type or AUTHORIZATION_AUTONOMY_DECISION,
            authorization_id=authorization_id or "",
            idempotency_key_hash=hash_idempotency_key(key) if key else "",
            attempt=1,
            started_at=started_at,
            completed_at=None,
            outcome=OUTCOME_KNOWN_FAILURE,
            resource_ref=str(action.resource or "") or None,
            reversible=True,
            version=1,
            permit_id=permit_id,
            approval_id=approval_id,
            tenant_id=str(tenant_id or ""),
            metadata={"phase": "started"},
        )
        try:
            uow = None
            if use_uow and self.persistence is not None:
                uow = self.persistence.unit_of_work()
            if uow is not None:
                with uow:
                    existing = self.store.get(execution_id)
                    if existing is None:
                        self.store.create(record)
                    else:
                        self.store.save(record)
                    if hasattr(self.idempotency, "bind_execution"):
                        self.idempotency.bind_execution(key, execution_id)
            else:
                existing = self.store.get(execution_id)
                if existing is None:
                    self.store.create(record)
                else:
                    self.store.save(record)
                if hasattr(self.idempotency, "bind_execution"):
                    if use_uow:
                        try:
                            self.idempotency.bind_execution(key, execution_id)
                        except Exception:
                            pass
                    else:
                        self.idempotency.bind_execution(key, execution_id)
        except SideEffectPersistenceUnavailableError:
            raise
        except SideEffectExecutionDeniedError:
            raise
        except Exception as exc:
            raise SideEffectPersistenceUnavailableError() from exc

    def _finalize_success(
        self,
        result,
        authorization_type,
        authorization_id,
        action,
        attempt: int,
        *,
        tenant_id: str = "",
    ) -> None:
        uow = None
        if self.persistence is not None:
            uow = self.persistence.unit_of_work()
        if uow is not None:
            with uow:
                self._save_record(
                    result,
                    authorization_type,
                    authorization_id,
                    action,
                    attempt,
                    tenant_id=tenant_id,
                )
                self.idempotency.mark_completed(action.idempotency_key)
        else:
            self._save_record(
                result,
                authorization_type,
                authorization_id,
                action,
                attempt,
                tenant_id=tenant_id,
            )
            self.idempotency.mark_completed(action.idempotency_key)

    def _save_record(
        self,
        result,
        authorization_type,
        authorization_id,
        action,
        attempt: int,
        *,
        tenant_id: str = "",
    ):
        key = action.idempotency_key or ""
        existing = self.store.get(result.execution_id)
        next_version = 1 if existing is None else int(existing.version) + 1
        resolved_tenant = str(tenant_id or "")
        if not resolved_tenant and existing is not None:
            resolved_tenant = str(existing.tenant_id or "")
        record = SideEffectExecutionRecord(
            execution_id=result.execution_id,
            action_id=result.action_id,
            workflow_id=result.workflow_id,
            task_id=result.task_id,
            tool_id=result.tool_id,
            operation=result.operation,
            status=result.status,
            authorization_type=authorization_type or AUTHORIZATION_AUTONOMY_DECISION,
            authorization_id=authorization_id or "",
            idempotency_key_hash=hash_idempotency_key(key) if key else "",
            attempt=attempt,
            started_at=result.started_at,
            completed_at=result.completed_at,
            error_code=result.error_code,
            external_reference=result.external_reference,
            rollback_status=(
                existing.rollback_status if existing is not None else ROLLBACK_NONE
            ),
            rollback_reference=result.rollback_reference,
            outcome=result.outcome,
            parent_execution_id=(
                existing.parent_execution_id if existing is not None else None
            ),
            reconciliation_id=(
                existing.reconciliation_id if existing is not None else None
            ),
            recovery_attempt=(
                existing.recovery_attempt if existing is not None else 0
            ),
            resource_ref=str(getattr(action, "resource", "") or "") or (
                existing.resource_ref if existing is not None else None
            ),
            reversible=bool(result.reversible),
            version=next_version,
            permit_id=(
                existing.permit_id if existing is not None else None
            ),
            approval_id=(
                existing.approval_id if existing is not None else None
            ),
            tenant_id=resolved_tenant,
            metadata={
                "authorization_type": authorization_type,
                "attempt": attempt,
                **dict(result.metadata or {}),
            },
        )
        if existing is None:
            self.store.create(record)
        else:
            self.store.save(record)

    def _result_from_record(self, record: SideEffectExecutionRecord) -> SideEffectExecutionResult:
        outcome = record.outcome
        status = record.status
        if status == STATUS_SUCCEEDED:
            outcome = OUTCOME_KNOWN_SUCCESS
        return SideEffectExecutionResult(
            execution_id=record.execution_id,
            workflow_id=record.workflow_id,
            task_id=record.task_id,
            action_id=record.action_id,
            tool_id=record.tool_id,
            operation=record.operation,
            status=status,
            started_at=record.started_at,
            completed_at=record.completed_at or record.started_at,
            outcome=outcome,
            external_reference=record.external_reference,
            reversible=bool(record.rollback_reference),
            rollback_reference=record.rollback_reference,
            error_code=record.error_code,
            metadata={"replay": True, "rollback_status": record.rollback_status},
        )

    def _fail_workflow(self, state_manager, workflow_id: str, error_code: str) -> None:
        if state_manager is None:
            return
        try:
            state = state_manager.get(workflow_id)
        except Exception:
            return
        if state.status in TERMINAL_STATUSES:
            return
        current = state.current_step or "route"
        try:
            state_manager.fail_step(workflow_id, current, error_code)
        except Exception:
            return
