"""UnifiedToolExecutor — builds trusted requests and delegates to ToolGateway."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping

from autonomy.models import sanitize_metadata, utc_now
from tools.failure import classify_exception
from tools.models import (
    TOOL_STATUS_FAILED,
    ToolRequest,
    ToolResult,
)
from tools.permissions import authorize_tool_request
from tools.registry import ToolRegistry
from tools.router import ToolRouter
from tools.schema_validation import validate_tool_args


class UnifiedToolExecutor:
    """Core-facing executor: Registry → Router → Permissions → Gateway (never adapters)."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        gateway,
        router: ToolRouter | None = None,
    ):
        self.registry = registry
        self.gateway = gateway
        self.router = router or getattr(gateway, "router", None) or ToolRouter(registry)

    def build_trusted_request(
        self,
        *,
        tool_id: str,
        operation: str,
        arguments: Mapping[str, Any] | None = None,
        envelope=None,
        context: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        tool_version: str = "",
        idempotency_key: str | None = None,
        dry_run: bool = False,
        requested_capabilities: tuple[str, ...] = (),
        capability_context: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolRequest:
        """Build ToolRequest; RunEnvelope / trusted context wins over payload identity."""
        ctx = dict(context or {})
        args = dict(arguments or {})
        # Strip any attempt to override identity via payload
        for forbidden in (
            "tenant_id",
            "user_id",
            "actor_id",
            "execution_id",
            "workflow_id",
            "correlation_id",
            "envelope_ref",
        ):
            args.pop(forbidden, None)

        if envelope is not None:
            workflow_id = str(getattr(envelope, "workflow_id", "") or "")
            task_id = str(getattr(envelope, "task_id", "") or ctx.get("task_id") or "")
            tenant_id = str(getattr(envelope, "tenant_id", "") or "")
            user_id = str(getattr(envelope, "user_id", "") or "")
            actor_id = str(
                getattr(envelope, "actor_ref", "") or ctx.get("actor_id") or ""
            )
            correlation_id = str(getattr(envelope, "correlation_id", "") or "")
            execution_id = str(getattr(envelope, "execution_id", "") or "")
            data_scope_ref = str(getattr(envelope, "data_scope_ref", "") or "")
            capability_scope_ref = str(
                getattr(envelope, "capability_scope_ref", "") or ""
            )
            deadline = getattr(envelope, "deadline_at", None)
            envelope_ref = str(getattr(envelope, "execution_id", "") or "")
            req_id = request_id or str(getattr(envelope, "request_id", "") or uuid.uuid4())
            idem = idempotency_key or getattr(envelope, "idempotency_key", None)
        else:
            workflow_id = str(ctx.get("workflow_id") or "")
            task_id = str(ctx.get("task_id") or "")
            tenant_id = str(ctx.get("tenant_id") or "")
            user_id = str(ctx.get("user_id") or "")
            actor_id = str(ctx.get("actor_id") or "")
            correlation_id = str(ctx.get("correlation_id") or "")
            execution_id = str(ctx.get("execution_id") or "")
            data_scope_ref = str(ctx.get("data_scope_ref") or "")
            capability_scope_ref = str(ctx.get("capability_scope_ref") or "")
            deadline = ctx.get("deadline")
            envelope_ref = str(ctx.get("envelope_ref") or "")
            req_id = request_id or str(uuid.uuid4())
            idem = idempotency_key

        return ToolRequest(
            request_id=str(req_id),
            workflow_id=workflow_id,
            task_id=task_id,
            tool_id=tool_id,
            operation=operation,
            arguments=sanitize_metadata(args),
            actor_id=actor_id,
            requested_capabilities=tuple(requested_capabilities),
            idempotency_key=idem,
            dry_run=bool(dry_run),
            created_at=utc_now(),
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            capability_context=capability_context,
            metadata=sanitize_metadata(dict(metadata or {})),
            tool_version=str(tool_version or ctx.get("tool_version") or ""),
            execution_id=execution_id,
            data_scope_ref=data_scope_ref,
            capability_scope_ref=capability_scope_ref,
            deadline=deadline if isinstance(deadline, datetime) else None,
            envelope_ref=envelope_ref,
        )

    async def execute(
        self,
        *,
        tool_id: str,
        operation: str,
        arguments: Mapping[str, Any] | None = None,
        envelope=None,
        context: Mapping[str, Any] | None = None,
        capabilities=None,
        tool_version: str = "",
        **gateway_kwargs,
    ) -> ToolResult:
        request = self.build_trusted_request(
            tool_id=tool_id,
            operation=operation,
            arguments=arguments,
            envelope=envelope,
            context=context,
            tool_version=tool_version,
            requested_capabilities=tuple(
                getattr(capabilities, "capabilities", ()) if capabilities is not None else ()
            ),
            idempotency_key=gateway_kwargs.pop("idempotency_key", None),
            dry_run=bool(gateway_kwargs.pop("dry_run", False)),
            metadata=gateway_kwargs.pop("metadata", None),
        )
        audit = getattr(self.gateway, "audit", None)
        if audit is not None:
            audit.record(
                "tool.requested",
                request_id=request.request_id,
                tool_id=request.tool_id,
                operation=request.operation,
                execution_id=request.execution_id,
                correlation_id=request.correlation_id,
            )

        version_pin = str(request.tool_version or "").strip() or None
        row = self.registry.resolve(request.tool_id, version_pin)
        descriptor = row.descriptor

        # Schema validate before routing/gateway
        validate_tool_args(
            dict(request.arguments),
            descriptor,
            tool_version=version_pin,
        )

        route = self.router.route(
            request,
            capability=str(request.capability_context or "").strip() or None,
            capabilities=capabilities,
        )
        if audit is not None:
            audit.record(
                "tool.routed",
                request_id=request.request_id,
                tool_id=route.selected_tool,
                selected_version=route.selected_version,
                policy_decision=route.policy_decision,
            )

        auth = authorize_tool_request(
            request=request,
            descriptor=route.descriptor,
            capabilities=capabilities,
        )
        if audit is not None:
            audit.record(
                "tool.authorized",
                request_id=request.request_id,
                tool_id=route.selected_tool,
                reason_code=auth.reason_code,
            )

        try:
            result = await self.gateway.invoke(
                request,
                capabilities=capabilities,
                **gateway_kwargs,
            )
            return result
        except Exception as exc:
            info = classify_exception(exc)
            if audit is not None:
                audit.record(
                    "tool.failed",
                    request_id=request.request_id,
                    tool_id=request.tool_id,
                    reason_code=info.reason_code,
                    retryable=info.retryable,
                )
            return ToolResult(
                request_id=request.request_id,
                tool_id=request.tool_id,
                operation=request.operation,
                status=TOOL_STATUS_FAILED,
                success=False,
                error_code=info.error_code,
                error_message_safe=info.reason_code,
                retryable=info.retryable,
                reason_code=info.reason_code,
                execution_id=request.execution_id or None,
            )
