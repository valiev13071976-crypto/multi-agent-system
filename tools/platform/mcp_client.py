"""Real MCP (Model Context Protocol) wire client — Block 5.4 activation.

Implements the two governed operations the platform needs, as plain
JSON-RPC 2.0 over HTTP per the MCP spec (``tools/list`` for discovery,
``tools/call`` for invocation: https://modelcontextprotocol.io).

This module only knows how to *speak* the wire protocol to a single
already-allowlisted server. Every governance decision (server allowlist,
tool allowlist, trust level, "never Agent->MCP direct") stays owned by
``tools.platform.contracts.McpAdapter`` -- unchanged from before this
activation. Passing a transport into ``McpAdapter`` is what turns a given
server from permanently ``scaffold_only`` into a real, governed call;
omitting one preserves the pre-existing scaffold behavior exactly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx


class McpTransportError(Exception):
    """Raised for any transport-level MCP failure (network, protocol, malformed body)."""


@runtime_checkable
class McpTransport(Protocol):
    async def list_tools(self) -> list[dict]: ...

    async def call_tool(self, name: str, arguments: dict) -> dict: ...


class HttpMcpTransport:
    """Minimal MCP-over-HTTP JSON-RPC 2.0 client.

    Bounded timeout and response size; no retries (the governed
    ``McpAdapter``/``ToolGateway`` layer owns retry/fail-closed policy, not
    the transport). Fully testable offline via ``httpx.MockTransport`` --
    no live network or paid API calls required.
    """

    MAX_RESPONSE_BYTES = 256_000

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)
        self._next_id = 1

    async def _rpc(self, method: str, params: dict) -> dict:
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            resp = await self._client.post(self._base_url, json=payload)
        except httpx.HTTPError as exc:
            raise McpTransportError(f"mcp_transport_error:{type(exc).__name__}") from exc
        if len(resp.content) > self.MAX_RESPONSE_BYTES:
            raise McpTransportError("mcp_response_too_large")
        try:
            body = resp.json()
        except ValueError as exc:
            raise McpTransportError("mcp_invalid_json") from exc
        if resp.status_code >= 400:
            raise McpTransportError(f"mcp_http_{resp.status_code}")
        if not isinstance(body, dict):
            raise McpTransportError("mcp_malformed_body")
        if "error" in body:
            err = body.get("error") or {}
            raise McpTransportError(f"mcp_rpc_error:{err.get('code')}:{err.get('message')}")
        result = body.get("result")
        if not isinstance(result, dict):
            raise McpTransportError("mcp_malformed_result")
        return result

    async def list_tools(self) -> list[dict]:
        result = await self._rpc("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpTransportError("mcp_malformed_tools_list")
        return [dict(t) for t in tools if isinstance(t, dict)]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._rpc("tools/call", {"name": name, "arguments": dict(arguments or {})})

    async def aclose(self) -> None:
        await self._client.aclose()
