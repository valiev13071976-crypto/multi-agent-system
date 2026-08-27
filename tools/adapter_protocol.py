"""Unified tool adapter protocol — all external integrations implement this."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tools.models import ToolRequest, ToolResult


@runtime_checkable
class ToolAdapter(Protocol):
    """Canonical adapter interface for Tool Platform."""

    adapter_id: str

    def supports(self, tool_id: str) -> bool: ...

    def health(self) -> str: ...

    async def execute(self, request: ToolRequest, *, context: dict | None = None) -> ToolResult: ...

    async def cancel(self, request: ToolRequest) -> ToolResult | None: ...

    async def compensate(self, request: ToolRequest, *, context: dict | None = None) -> ToolResult | None: ...


@runtime_checkable
class ReadToolAdapter(Protocol):
    """Read-only adapter — used by ToolGateway._invoke_read."""

    async def execute_read(self, request: ToolRequest, context: dict) -> dict: ...
