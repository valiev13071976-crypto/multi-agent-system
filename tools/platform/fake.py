"""Deterministic FakeToolAdapter for tests (read/write/failures/cost/timeout/idempotency)."""

from __future__ import annotations

import asyncio
from typing import Any

from tools.errors import (
    ToolConflictError,
    ToolPermanentFailureError,
    ToolRateLimitedError,
    ToolTimeoutError,
    ToolTransientFailureError,
)
from tools.models import (
    ADAPTER_HEALTHY,
    ADAPTER_UNAVAILABLE,
    TOOL_STATUS_SUCCEEDED,
    ToolRequest,
    ToolResult,
)


class FakeToolAdapter:
    """Configurable fake adapter — never used as production vendor SDK."""

    adapter_id: str = "fake"

    def __init__(
        self,
        *,
        adapter_id: str = "fake",
        enabled: bool = True,
        fail_mode: str | None = None,
        delay_seconds: float = 0.0,
        cost_units: float = 0.0,
        read_payload: dict | None = None,
        write_store: dict | None = None,
    ):
        self.adapter_id = adapter_id
        self._enabled = enabled
        self.fail_mode = fail_mode
        self.delay_seconds = float(delay_seconds)
        self.cost_units = float(cost_units)
        self.read_payload = dict(read_payload or {"ok": True, "scaffold": True})
        self.write_store = write_store if write_store is not None else {}
        self.read_calls = 0
        self.write_calls = 0
        self.seen_idempotency: set[str] = set()

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith(f"{self.adapter_id}.") or tool_id == self.adapter_id

    def health(self) -> str:
        return ADAPTER_HEALTHY if self._enabled else ADAPTER_UNAVAILABLE

    async def _maybe_fail(self) -> None:
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        mode = str(self.fail_mode or "")
        if mode == "timeout":
            raise ToolTimeoutError()
        if mode == "rate_limited":
            raise ToolRateLimitedError()
        if mode == "transient":
            raise ToolTransientFailureError()
        if mode == "permanent":
            raise ToolPermanentFailureError()
        if mode == "conflict":
            raise ToolConflictError()

    async def execute_read(self, request: ToolRequest, context: dict) -> dict:
        self.read_calls += 1
        await self._maybe_fail()
        payload = dict(self.read_payload)
        payload.update(
            {
                "tool_id": request.tool_id,
                "operation": request.operation,
                "arguments": dict(request.arguments or {}),
                "usage": {"cost_units": self.cost_units},
                "provenance": {"adapter": self.adapter_id, "fake": True},
            }
        )
        return payload

    async def execute_write(self, request: ToolRequest, context: dict) -> dict:
        """Side-effect path helper — ToolGateway should still route via SideEffectExecutor."""
        self.write_calls += 1
        await self._maybe_fail()
        idem = str(request.idempotency_key or "")
        if idem and idem in self.seen_idempotency:
            return {
                "idempotent_replay": True,
                "value": self.write_store.get(idem),
                "provenance": {"adapter": self.adapter_id, "fake": True},
            }
        value = dict(request.arguments or {}).get("value")
        if idem:
            self.seen_idempotency.add(idem)
            self.write_store[idem] = value
        resource = str(dict(request.arguments or {}).get("resource") or "default")
        self.write_store[resource] = value
        return {
            "written": True,
            "resource": resource,
            "value": value,
            "usage": {"cost_units": self.cost_units},
            "provenance": {"adapter": self.adapter_id, "fake": True},
        }

    async def execute(self, request: ToolRequest, *, context: dict | None = None) -> ToolResult:
        ctx = dict(context or {})
        if request.operation.startswith("write") or ctx.get("write"):
            data = await self.execute_write(request, ctx)
        else:
            data = await self.execute_read(request, ctx)
        return ToolResult(
            request_id=request.request_id,
            tool_id=request.tool_id,
            operation=request.operation,
            status=TOOL_STATUS_SUCCEEDED,
            success=True,
            data=data,
            adapter_id=self.adapter_id,
            usage=dict(data.get("usage") or {}),
            provenance=dict(data.get("provenance") or {}),
        )

    # SideEffectAdapter-compatible surface for write E2E tests
    async def execute_side_effect(self, action, context: dict | None = None) -> dict:
        req = ToolRequest(
            request_id=str(getattr(action, "action_id", "") or "fake"),
            workflow_id=str(getattr(action, "workflow_id", "") or ""),
            task_id=str(getattr(action, "task_id", "") or ""),
            tool_id=str(getattr(action, "tool_id", "") or self.adapter_id),
            operation=str(getattr(action, "operation", "") or "write"),
            arguments=dict(getattr(action, "arguments", {}) or {}),
            idempotency_key=getattr(action, "idempotency_key", None),
            tenant_id=str(getattr(action, "tenant_id", "") or ""),
            actor_id=str(getattr(action, "actor_id", "") or ""),
        )
        return await self.execute_write(req, dict(context or {}))
