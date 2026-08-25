"""ToolGateway — single controlled tool execution boundary.

Preserves existing READ_ONLY_EXTERNAL search() semantics while adding
registry-backed invoke() for read and write tools.
Write tools never call adapters directly; they go through SideEffectExecutor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone

from autonomy.models import (
    ACTION_READ,
    ACTION_WRITE,
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
    DECISION_REVIEW_AFTER,
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_UNCERTAIN,
    sanitize_metadata,
    utc_now,
)
from security.redaction import redact
from tools.adapters import SearchReadAdapter, search_tool_descriptor
from tools.errors import (
    ToolArgumentInvalidError,
    ToolCapabilityError,
    ToolDisabledError,
    ToolError,
    ToolIdempotencyRequiredError,
    ToolNotFoundError,
    ToolOperationNotAllowedError,
    ToolPolicyDeniedError,
    ToolSideEffectUncertainError,
    ToolTimeoutError,
)
from tools.models import (
    DEFAULT_SEARCH_TIMEOUT_SECONDS,
    FORBIDDEN_BYPASS_KEYS,
    FORBIDDEN_DYNAMIC_KEYS,
    MAX_SEARCH_RESULTS_PER_CLAIM,
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_ARGUMENT_DEPTH,
    MAX_TOOL_ARGUMENT_LIST_LEN,
    MAX_TOOL_ARGUMENT_STRING_LEN,
    MAX_TOOL_RESULT_DATA_BYTES,
    MAX_TOTAL_SEARCH_RESULTS,
    SEARCH_TOOL_ID,
    TOOL_STATUS_APPROVAL_REQUIRED,
    TOOL_STATUS_DENIED,
    TOOL_STATUS_FAILED,
    TOOL_STATUS_SUCCEEDED,
    TOOL_STATUS_UNCERTAIN,
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_PRIVILEGED,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    SearchResult,
    ToolDescriptor,
    ToolRequest,
    ToolResult,
    ToolUsageRecord,
)
from tools.observability import ToolMetrics
from tools.registry import ToolRegistry
from tools.search.http_provider import SearchUnavailableError
from tools.search.null_provider import NullSearchProvider
from tools.trust import trust_for_domain
from tools.url_safety import UnsafeUrlError, is_safe_http_url, source_domain, validate_http_url


class SearchTimeoutError(TimeoutError):
    pass


EVENT_TOOL_REQUESTED = "tool.requested"
EVENT_TOOL_DENIED = "tool.denied"
EVENT_TOOL_READ_STARTED = "tool.read_started"
EVENT_TOOL_READ_COMPLETED = "tool.read_completed"
EVENT_TOOL_WRITE_PROPOSED = "tool.write_proposed"
EVENT_TOOL_APPROVAL_REQUIRED = "tool.approval_required"
EVENT_TOOL_WRITE_STARTED = "tool.write_started"
EVENT_TOOL_WRITE_COMPLETED = "tool.write_completed"
EVENT_TOOL_FAILED = "tool.failed"
EVENT_TOOL_UNCERTAIN = "tool.uncertain"


class ToolAuditLog:
    def __init__(self):
        self._events: list[dict] = []

    def record(self, event_type: str, **payload) -> None:
        row = {"event_type": event_type, **sanitize_metadata(payload)}
        self._events.append(row)

    def list_all(self) -> tuple[dict, ...]:
        return tuple(self._events)


def _json_size(value) -> int:
    try:
        return len(
            json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError):
        return MAX_TOOL_ARGUMENT_BYTES + 1


def validate_tool_arguments(arguments: dict | None) -> dict:
    data = dict(arguments or {})
    for key in list(data.keys()):
        lowered = str(key).lower()
        if lowered in FORBIDDEN_BYPASS_KEYS or lowered in FORBIDDEN_DYNAMIC_KEYS:
            raise ToolArgumentInvalidError("tool_argument_invalid")
    if _json_size(data) > MAX_TOOL_ARGUMENT_BYTES:
        raise ToolArgumentInvalidError("tool_argument_invalid")

    def _walk(node, depth: int) -> None:
        if depth > MAX_TOOL_ARGUMENT_DEPTH:
            raise ToolArgumentInvalidError("tool_argument_invalid")
        if isinstance(node, str) and len(node) > MAX_TOOL_ARGUMENT_STRING_LEN:
            raise ToolArgumentInvalidError("tool_argument_invalid")
        if isinstance(node, list):
            if len(node) > MAX_TOOL_ARGUMENT_LIST_LEN:
                raise ToolArgumentInvalidError("tool_argument_invalid")
            for item in node:
                _walk(item, depth + 1)
        elif isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in FORBIDDEN_BYPASS_KEYS | FORBIDDEN_DYNAMIC_KEYS:
                    raise ToolArgumentInvalidError("tool_argument_invalid")
                _walk(value, depth + 1)

    _walk(data, 0)
    return sanitize_metadata(data)


def bound_result_data(data: dict | None) -> dict:
    cleaned = sanitize_metadata(data or {})
    if _json_size(cleaned) <= MAX_TOOL_RESULT_DATA_BYTES:
        return cleaned
    return {"truncated": True, "keys": sorted(cleaned.keys())[:32]}


def action_fingerprint_for_tool(
    *,
    tool_id: str,
    operation: str,
    arguments: dict,
    descriptor_version: str,
    schema_hash: str = "",
) -> str:
    payload = {
        "tool_id": tool_id,
        "operation": operation,
        "arguments": arguments,
        "descriptor_version": descriptor_version,
        "schema_hash": schema_hash,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ToolGateway:
    """
    Single external access point for tools.

    Legacy: search() remains READ_ONLY_EXTERNAL-only.
    Unified: invoke(ToolRequest) routes read/write with policy enforcement.
    """

    def __init__(
        self,
        search_provider=None,
        *,
        timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
        max_results_per_call: int = MAX_SEARCH_RESULTS_PER_CLAIM,
        max_total_results: int = MAX_TOTAL_SEARCH_RESULTS,
        tool_trust_level: str = TOOL_TRUST_READ_ONLY_EXTERNAL,
        task_id: str = "",
        registry: ToolRegistry | None = None,
        side_effect_executor=None,
        gate=None,
        hitl=None,
        audit: ToolAuditLog | None = None,
        metrics: ToolMetrics | None = None,
        register_search: bool = True,
    ):
        self._provider = search_provider or NullSearchProvider()
        self._timeout_seconds = float(timeout_seconds)
        self._max_results_per_call = int(max_results_per_call)
        self._max_total_results = int(max_total_results)
        self.tool_trust_level = tool_trust_level
        self.task_id = task_id
        self.last_usage: list[ToolUsageRecord] = []
        self._total_results = 0
        self.queries: list[str] = []
        self.registry = registry or ToolRegistry()
        self.side_effect_executor = side_effect_executor
        self.gate = gate
        self.hitl = hitl
        self.audit = audit or ToolAuditLog()
        self.metrics = metrics or ToolMetrics()
        if register_search and SEARCH_TOOL_ID not in {
            d.tool_id for d in self.registry.list_tools(include_disabled=True)
        }:
            self.registry.register(
                search_tool_descriptor(), adapter=SearchReadAdapter(self)
            )

    def reset_budget(self) -> None:
        self._total_results = 0

    def remaining_results(self) -> int:
        return max(0, self._max_total_results - self._total_results)

    def list_tools(self, *, include_disabled: bool = False) -> tuple[ToolDescriptor, ...]:
        return self.registry.list_tools(include_disabled=include_disabled)

    def get_tool(self, tool_id: str) -> ToolDescriptor:
        return self.registry.get(tool_id)

    def list_operations(self, tool_id: str) -> tuple[str, ...]:
        return self.registry.list_operations(tool_id)

    def health(self) -> dict:
        return dict(self.registry.health())

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        if self.tool_trust_level != TOOL_TRUST_READ_ONLY_EXTERNAL:
            raise SearchUnavailableError("tool_trust_not_allowed")
        cleaned = redact(str(query or "")).strip()
        if not cleaned or cleaned == "[REDACTED]":
            return []
        remaining = self.remaining_results()
        if remaining <= 0:
            return []
        limit = min(int(max_results), self._max_results_per_call, remaining)
        if limit <= 0:
            return []
        self.queries.append(cleaned)
        started = datetime.now(timezone.utc)
        success = False
        try:
            rows = await asyncio.wait_for(
                self._provider.search(cleaned, max_results=limit),
                timeout=self._timeout_seconds,
            )
            safe = []
            for item in rows or []:
                if not isinstance(item, SearchResult):
                    continue
                if not is_safe_http_url(item.url):
                    continue
                try:
                    validate_http_url(item.url)
                except UnsafeUrlError:
                    continue
                domain = source_domain(item.url) or item.source_domain
                item = SearchResult(
                    title=item.title,
                    url=item.url,
                    snippet=item.snippet,
                    source_domain=domain,
                    published_at=item.published_at,
                    retrieved_at=item.retrieved_at,
                    trust_level=trust_for_domain(domain),
                )
                safe.append(item)
                if len(safe) >= limit:
                    break
            self._total_results += len(safe)
            success = True
            return safe
        except asyncio.TimeoutError as exc:
            raise SearchTimeoutError("external_evidence_timeout") from exc
        except SearchTimeoutError:
            raise
        except SearchUnavailableError:
            raise
        except Exception as exc:
            raise SearchUnavailableError(redact(str(exc))) from exc
        finally:
            elapsed = datetime.now(timezone.utc) - started
            self.last_usage.append(
                ToolUsageRecord(
                    tool_id="search",
                    task_id=self.task_id,
                    operation="search",
                    timestamp=started,
                    success=success,
                    latency_ms=int(elapsed.total_seconds() * 1000),
                    metadata={"query_len": len(cleaned)},
                )
            )

    async def invoke(
        self,
        request: ToolRequest,
        *,
        capabilities=None,
        gate=None,
        hitl=None,
        executor=None,
        state_manager=None,
        permit=None,
        decision=None,
        now=None,
        evaluate_kwargs=None,
    ) -> ToolResult:
        stamp = now or utc_now()
        started = stamp
        gate = gate or self.gate
        hitl = hitl if hitl is not None else self.hitl
        executor = executor if executor is not None else self.side_effect_executor
        self.audit.record(
            EVENT_TOOL_REQUESTED,
            request_id=request.request_id,
            tool_id=request.tool_id,
            operation=request.operation,
            workflow_id=request.workflow_id,
            task_id=request.task_id,
            dry_run=bool(request.dry_run),
        )
        try:
            self._reject_bypass(request)
            args = validate_tool_arguments(dict(request.arguments))
            descriptor = self.registry.get(request.tool_id)
            if not descriptor.enabled:
                raise ToolDisabledError()
            if request.operation not in descriptor.operations:
                raise ToolOperationNotAllowedError()
            self._require_capabilities(descriptor, request, capabilities)
            if descriptor.trust_level == TOOL_TRUST_PRIVILEGED:
                raise ToolPolicyDeniedError("tool_policy_denied")
            if descriptor.trust_level == TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE:
                raise ToolPolicyDeniedError("tool_policy_denied")
            if descriptor.read_only:
                result = await self._invoke_read(
                    request, descriptor, args, started=started, stamp=stamp
                )
            else:
                result = await self._invoke_write(
                    request,
                    descriptor,
                    args,
                    gate=gate,
                    hitl=hitl,
                    executor=executor,
                    state_manager=state_manager,
                    permit=permit,
                    decision=decision,
                    stamp=stamp,
                    started=started,
                    evaluate_kwargs=evaluate_kwargs,
                    capabilities=capabilities,
                )
            self._record_metrics(descriptor, result)
            return result
        except ToolError as exc:
            duration = int((utc_now() - started).total_seconds() * 1000)
            status = TOOL_STATUS_DENIED
            outcome = "denied"
            if exc.error_code in {"tool_timeout"}:
                status = TOOL_STATUS_FAILED
                outcome = "timeout"
            elif exc.error_code in {"tool_side_effect_uncertain"}:
                status = TOOL_STATUS_UNCERTAIN
                outcome = "uncertain"
            elif exc.error_code in {"tool_execution_failed"}:
                status = TOOL_STATUS_FAILED
                outcome = "failure"
            result = ToolResult(
                request_id=request.request_id,
                tool_id=request.tool_id,
                operation=request.operation,
                status=status,
                success=False,
                error_code=exc.error_code,
                error_message_safe=redact(str(exc.error_code)),
                trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL,
                duration_ms=duration,
            )
            self.audit.record(
                EVENT_TOOL_DENIED if outcome == "denied" else EVENT_TOOL_FAILED,
                request_id=request.request_id,
                tool_id=request.tool_id,
                operation=request.operation,
                error_code=exc.error_code,
            )
            trust = TOOL_TRUST_READ_ONLY_EXTERNAL
            try:
                trust = self.registry.get(request.tool_id).trust_level
            except ToolNotFoundError:
                pass
            self.metrics.record(
                tool_id=request.tool_id,
                operation=request.operation,
                trust_level=trust,
                outcome=outcome,
                latency_ms=duration,
            )
            return result
        except Exception as exc:
            duration = int((utc_now() - started).total_seconds() * 1000)
            self.audit.record(
                EVENT_TOOL_FAILED,
                request_id=request.request_id,
                tool_id=request.tool_id,
                operation=request.operation,
                error_code="tool_execution_failed",
            )
            self.metrics.record(
                tool_id=request.tool_id,
                operation=request.operation,
                trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL,
                outcome="failure",
                latency_ms=duration,
            )
            return ToolResult(
                request_id=request.request_id,
                tool_id=request.tool_id,
                operation=request.operation,
                status=TOOL_STATUS_FAILED,
                success=False,
                error_code="tool_execution_failed",
                error_message_safe=redact(str(exc.__class__.__name__)),
                duration_ms=duration,
            )

    def _reject_bypass(self, request: ToolRequest) -> None:
        meta = dict(request.metadata or {})
        args = dict(request.arguments or {})
        for key in list(meta.keys()) + list(args.keys()):
            if str(key).lower() in FORBIDDEN_BYPASS_KEYS:
                raise ToolPolicyDeniedError("tool_policy_denied")

    def _require_capabilities(self, descriptor, request, capabilities) -> None:
        required = set(descriptor.capabilities_required)
        if not required:
            return
        provided = set(request.requested_capabilities or ())
        if capabilities is not None and hasattr(capabilities, "capabilities"):
            provided |= set(capabilities.capabilities)
        if not required <= provided:
            raise ToolCapabilityError()

    async def _invoke_read(
        self, request, descriptor, args, *, started, stamp
    ) -> ToolResult:
        self.audit.record(
            EVENT_TOOL_READ_STARTED,
            request_id=request.request_id,
            tool_id=descriptor.tool_id,
            operation=request.operation,
        )
        registration = self.registry.get_registration(descriptor.tool_id)
        adapter = registration.adapter
        if adapter is None or not hasattr(adapter, "execute_read"):
            raise ToolError("tool_execution_failed")
        timeout = float(descriptor.timeout_seconds or self._timeout_seconds)
        try:
            data = await asyncio.wait_for(
                adapter.execute_read(request, {"now": stamp, "arguments": args}),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ToolTimeoutError() from exc
        duration = int((utc_now() - started).total_seconds() * 1000)
        result = ToolResult(
            request_id=request.request_id,
            tool_id=descriptor.tool_id,
            operation=request.operation,
            status=TOOL_STATUS_SUCCEEDED,
            success=True,
            data=bound_result_data(data if isinstance(data, dict) else {"value": data}),
            trust_level=descriptor.trust_level,
            side_effect=False,
            duration_ms=duration,
        )
        self.audit.record(
            EVENT_TOOL_READ_COMPLETED,
            request_id=request.request_id,
            tool_id=descriptor.tool_id,
            operation=request.operation,
            duration_ms=duration,
        )
        return result

    async def _invoke_write(
        self,
        request,
        descriptor,
        args,
        *,
        gate,
        hitl,
        executor,
        state_manager,
        permit,
        decision,
        stamp,
        started,
        evaluate_kwargs,
        capabilities,
    ) -> ToolResult:
        if descriptor.read_only:
            raise ToolPolicyDeniedError()
        if descriptor.idempotency_required and not request.idempotency_key:
            raise ToolIdempotencyRequiredError()
        action = self._normalize_write_action(request, descriptor, args)
        self.audit.record(
            EVENT_TOOL_WRITE_PROPOSED,
            request_id=request.request_id,
            tool_id=descriptor.tool_id,
            operation=request.operation,
            action_id=action.action_id,
        )
        if request.dry_run:
            return await self._dry_run_write(
                request,
                descriptor,
                action,
                args,
                gate=gate,
                executor=executor,
                stamp=stamp,
                started=started,
                evaluate_kwargs=evaluate_kwargs,
                capabilities=capabilities,
            )
        if executor is None:
            raise ToolError("tool_execution_failed")
        from autonomy.gate import AutonomyGate

        gate = gate or AutonomyGate()
        kwargs = dict(evaluate_kwargs or {})
        kwargs.setdefault("now", stamp)
        if capabilities is not None:
            kwargs.setdefault("capabilities", capabilities)

        # Idempotency duplicate handling before mutation.
        if action.idempotency_key:
            existing = gate.idempotency.get(action.idempotency_key)
            if existing is not None and existing.state == IDEMPOTENCY_COMPLETED:
                duration = int((utc_now() - started).total_seconds() * 1000)
                return ToolResult(
                    request_id=request.request_id,
                    tool_id=descriptor.tool_id,
                    operation=request.operation,
                    status=TOOL_STATUS_SUCCEEDED,
                    success=True,
                    data={"duplicate": True, "execution_id": existing.execution_id},
                    trust_level=descriptor.trust_level,
                    side_effect=True,
                    execution_id=existing.execution_id,
                    duration_ms=duration,
                    metadata={"idempotency": "completed"},
                )
            if existing is not None and existing.state == IDEMPOTENCY_UNCERTAIN:
                raise ToolSideEffectUncertainError()

        current_decision = decision
        if current_decision is None and permit is None:
            current_decision = gate.evaluate(action, **kwargs)
        if permit is None and current_decision is not None:
            if current_decision.decision == DECISION_DENY:
                raise ToolPolicyDeniedError()
            needs_approval = current_decision.decision == DECISION_REQUIRE_APPROVAL or (
                current_decision.decision == DECISION_REVIEW_AFTER
                and descriptor.trust_level != TOOL_TRUST_INTERNAL_SAFE
            )
            if needs_approval:
                approval_id = None
                if hitl is not None:
                    # Promote review_after external writes to explicit approval request.
                    from autonomy.models import AutonomyDecision

                    approval_decision = current_decision
                    if current_decision.decision == DECISION_REVIEW_AFTER:
                        approval_decision = AutonomyDecision(
                            decision_id=current_decision.decision_id,
                            action_id=current_decision.action_id,
                            decision=DECISION_REQUIRE_APPROVAL,
                            risk_class=current_decision.risk_class,
                            reason_code=current_decision.reason_code,
                            required_approval=True,
                            capabilities_checked=current_decision.capabilities_checked,
                            idempotency_required=current_decision.idempotency_required,
                            idempotency_satisfied=current_decision.idempotency_satisfied,
                            tool_trust_level=current_decision.tool_trust_level,
                            timestamp=current_decision.timestamp,
                        )
                    record = hitl.request_approval(
                        action,
                        approval_decision,
                        requested_by=request.actor_id or "agent",
                        now=stamp,
                    )
                    approval_id = record.approval_id
                self.audit.record(
                    EVENT_TOOL_APPROVAL_REQUIRED,
                    request_id=request.request_id,
                    tool_id=descriptor.tool_id,
                    approval_id=approval_id,
                )
                duration = int((utc_now() - started).total_seconds() * 1000)
                return ToolResult(
                    request_id=request.request_id,
                    tool_id=descriptor.tool_id,
                    operation=request.operation,
                    status=TOOL_STATUS_APPROVAL_REQUIRED,
                    success=False,
                    error_code="tool_approval_required",
                    error_message_safe="tool_approval_required",
                    trust_level=descriptor.trust_level,
                    side_effect=True,
                    approval_id=approval_id,
                    duration_ms=duration,
                )
            if current_decision.decision != DECISION_ALLOW:
                raise ToolPolicyDeniedError()

        self.audit.record(
            EVENT_TOOL_WRITE_STARTED,
            request_id=request.request_id,
            tool_id=descriptor.tool_id,
            operation=request.operation,
        )
        from side_effects.models import SideEffectExecutionContext

        context = SideEffectExecutionContext(payload=dict(args), now=stamp)
        try:
            se_result = await executor.execute(
                action,
                decision=current_decision,
                permit=permit,
                context=context,
                gate=gate,
                hitl=hitl,
                state_manager=state_manager,
                now=stamp,
                evaluate_kwargs=kwargs,
                timeout_seconds=float(descriptor.timeout_seconds),
            )
        except Exception as exc:
            code = getattr(exc, "error_code", None) or "tool_execution_failed"
            if "uncertain" in str(code):
                raise ToolSideEffectUncertainError() from exc
            if "timeout" in str(code):
                raise ToolTimeoutError() from exc
            if "permit" in str(code):
                from tools.errors import ToolPermitInvalidError

                raise ToolPermitInvalidError(str(code)) from exc
            if "persist" in str(code):
                from tools.errors import ToolPersistenceUnavailableError

                raise ToolPersistenceUnavailableError(str(code)) from exc
            raise ToolError(str(code)) from exc

        duration = int((utc_now() - started).total_seconds() * 1000)
        uncertain = getattr(se_result, "outcome", "") == "uncertain" or getattr(
            se_result, "status", ""
        ) in {"unknown"}
        if uncertain:
            self.audit.record(
                EVENT_TOOL_UNCERTAIN,
                request_id=request.request_id,
                tool_id=descriptor.tool_id,
                execution_id=se_result.execution_id,
            )
            return ToolResult(
                request_id=request.request_id,
                tool_id=descriptor.tool_id,
                operation=request.operation,
                status=TOOL_STATUS_UNCERTAIN,
                success=False,
                error_code="tool_side_effect_uncertain",
                trust_level=descriptor.trust_level,
                side_effect=True,
                execution_id=se_result.execution_id,
                external_reference=getattr(se_result, "external_reference", None),
                duration_ms=duration,
            )
        success = getattr(se_result, "status", "") == "succeeded"
        self.audit.record(
            EVENT_TOOL_WRITE_COMPLETED,
            request_id=request.request_id,
            tool_id=descriptor.tool_id,
            execution_id=se_result.execution_id,
            success=success,
        )
        return ToolResult(
            request_id=request.request_id,
            tool_id=descriptor.tool_id,
            operation=request.operation,
            status=TOOL_STATUS_SUCCEEDED if success else TOOL_STATUS_FAILED,
            success=success,
            data=bound_result_data(
                {
                    "execution_id": se_result.execution_id,
                    "external_reference": getattr(se_result, "external_reference", None),
                }
            ),
            trust_level=descriptor.trust_level,
            side_effect=True,
            execution_id=se_result.execution_id,
            permit_id=getattr(permit, "permit_id", None) if permit is not None else None,
            external_reference=getattr(se_result, "external_reference", None),
            duration_ms=duration,
        )

    async def _dry_run_write(
        self,
        request,
        descriptor,
        action,
        args,
        *,
        gate,
        executor,
        stamp,
        started,
        evaluate_kwargs,
        capabilities,
    ) -> ToolResult:
        from autonomy.gate import AutonomyGate

        gate = gate or AutonomyGate()
        kwargs = dict(evaluate_kwargs or {})
        kwargs.setdefault("now", stamp)
        if capabilities is not None:
            kwargs.setdefault("capabilities", capabilities)
        preview_gate = AutonomyGate(
            policy=gate.policy,
            classifier=gate.classifier,
            autonomy_level=kwargs.get("autonomy_level") or gate.autonomy_level,
        )
        preview_kwargs = dict(kwargs)
        preview_kwargs.pop("now", None)
        preview = preview_gate.evaluate(action, now=stamp, **preview_kwargs)
        would_execute = preview.decision == DECISION_ALLOW
        adapter_preview = {}
        if executor is not None and hasattr(executor, "dry_run"):
            try:
                from side_effects.models import SideEffectExecutionContext

                planned = await executor.dry_run(
                    action,
                    context=SideEffectExecutionContext(payload=dict(args), now=stamp),
                    gate=gate,
                    now=stamp,
                    evaluate_kwargs=kwargs,
                )
                adapter_preview = {
                    "would_execute": getattr(planned, "would_execute", False),
                    "would_change": getattr(planned, "would_change", None),
                    "would_require_approval": getattr(
                        planned, "would_require_approval", False
                    ),
                }
                would_execute = bool(adapter_preview.get("would_execute", would_execute))
            except Exception:
                adapter_preview = {"preview_error": True}
        duration = int((utc_now() - started).total_seconds() * 1000)
        return ToolResult(
            request_id=request.request_id,
            tool_id=descriptor.tool_id,
            operation=request.operation,
            status=TOOL_STATUS_SUCCEEDED,
            success=True,
            data=bound_result_data(
                {
                    "dry_run": True,
                    "would_execute": would_execute,
                    "policy_decision": preview.decision,
                    "policy_reason": preview.reason_code,
                    **adapter_preview,
                }
            ),
            trust_level=descriptor.trust_level,
            side_effect=False,
            duration_ms=duration,
            metadata={"mutation_calls": 0, "permit_consumed": False},
        )

    def _normalize_write_action(self, request, descriptor, args) -> object:
        from autonomy.gate import build_proposed_action
        from tools.models import TOOL_TRUST_INTERNAL_SAFE

        resource = str(
            args.get("resource")
            or request.metadata.get("resource")
            or descriptor.resource_prefix
            or descriptor.tool_id
        )
        action_type = ACTION_WRITE
        if descriptor.action_types_supported:
            action_type = descriptor.action_types_supported[0]
        meta = {
            "reversible": bool(descriptor.reversible),
            "tool_version": descriptor.version,
            "schema_hash": descriptor.schema_hash,
            "tool_request_id": request.request_id,
            "action_fingerprint": action_fingerprint_for_tool(
                tool_id=descriptor.tool_id,
                operation=request.operation,
                arguments=args,
                descriptor_version=descriptor.version,
                schema_hash=descriptor.schema_hash,
            ),
        }
        for key in ("owner", "repo", "issue_number", "label", "value"):
            if key in args:
                meta[key] = args[key]
        risk_class = None
        if (
            descriptor.trust_level == TOOL_TRUST_INTERNAL_SAFE
            and descriptor.reversible
        ):
            risk_class = "low"
        return build_proposed_action(
            action_type=action_type,
            tool_id=descriptor.tool_id,
            operation=request.operation,
            resource=resource,
            tool_trust_level=descriptor.trust_level,
            requested_capabilities=tuple(
                request.requested_capabilities or descriptor.capabilities_required
            ),
            idempotency_key=request.idempotency_key,
            workflow_id=request.workflow_id,
            task_id=request.task_id,
            metadata=meta,
            action_id=str(uuid.uuid4()),
            risk_class=risk_class,
        )

    def _record_metrics(self, descriptor, result: ToolResult) -> None:
        if result.status == TOOL_STATUS_SUCCEEDED:
            outcome = "success"
        elif result.status == TOOL_STATUS_DENIED:
            outcome = "denied"
        elif result.status == TOOL_STATUS_UNCERTAIN:
            outcome = "uncertain"
        elif result.error_code == "tool_timeout":
            outcome = "timeout"
        else:
            outcome = "failure"
        self.metrics.record(
            tool_id=descriptor.tool_id,
            operation=result.operation,
            trust_level=descriptor.trust_level,
            outcome=outcome,
            latency_ms=result.duration_ms,
        )
