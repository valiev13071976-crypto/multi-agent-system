"""PANDA — BLOCK 5.4 Tool & Integration Platform activation — targeted tests.

Activates the previously permanently-scaffold-only MCP bridge
(``tools/platform/contracts.py:McpAdapter``) with a real, governed JSON-RPC
2.0 transport (``tools/platform/mcp_client.py:HttpMcpTransport``), while
preserving every pre-existing governance check (server allowlist, tool
allowlist, trust level, "never Agent->MCP direct") and the pre-existing
``scaffold_only`` default behavior for any server with no transport wired.

Uses only local fixtures / an in-process ``httpx.MockTransport`` (no live
network, no paid model/API calls). Mirrors the Block 5.2 test conventions
(``tests/test_block5_2_acquisition_platform.py``) and the existing
``tests/test_tool_integration_platform.py`` MCP contract coverage, which
this activation must not regress.
"""

from __future__ import annotations

import json
import unittest

import httpx

from autonomy.capabilities import CAP_EXTERNAL_READ, CAP_MCP_INVOKE, CapabilitySet
from autonomy.models import utc_now
from tools.errors import ToolPolicyDeniedError, ToolUnavailableError
from tools.gateway import ToolGateway
from tools.models import TOOL_STATUS_SUCCEEDED, ToolRequest
from tools.platform.bootstrap import register_platform_tools
from tools.platform.contracts import McpAdapter
from tools.platform.mcp_client import HttpMcpTransport, McpTransportError
from tools.registry import ToolRegistry


def _rpc_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    method = body.get("method")
    rid = body.get("id")
    if method == "tools/list":
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "tools": [
                        {
                            "name": "search",
                            "description": "Search a fixture catalog",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                        }
                    ]
                },
            },
        )
    if method == "tools/call":
        params = body.get("params") or {}
        if params.get("name") != "search":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown tool"}},
            )
        query = (params.get("arguments") or {}).get("query", "")
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": f"echo:{query}"}], "isError": False},
            },
        )
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}}
    )


def _transport(handler=_rpc_handler) -> HttpMcpTransport:
    return HttpMcpTransport(base_url="https://mcp.fixture.test/rpc", transport=httpx.MockTransport(handler))


async def _read(adapter, tool_id, operation, **args):
    req = ToolRequest(
        request_id="r", workflow_id="w", task_id="t", tool_id=tool_id, operation=operation, arguments=args
    )
    return await adapter.execute_read(req, {})


class HttpMcpTransportTests(unittest.IsolatedAsyncioTestCase):
    """Real JSON-RPC 2.0 wire protocol (``tools/list`` / ``tools/call``), offline."""

    async def test_list_tools_parses_real_jsonrpc_response(self):
        tools = await _transport().list_tools()
        self.assertEqual(tools[0]["name"], "search")

    async def test_call_tool_returns_real_content(self):
        result = await _transport().call_tool("search", {"query": "panda"})
        self.assertEqual(result["content"][0]["text"], "echo:panda")

    async def test_rpc_error_field_raises_transport_error(self):
        with self.assertRaises(McpTransportError):
            await _transport().call_tool("nope", {})

    async def test_http_error_status_raises_transport_error(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        with self.assertRaises(McpTransportError):
            await _transport(handler).list_tools()

    async def test_oversized_response_rejected(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"tools": [{"name": "x", "blob": "y" * 300_000}]},
                },
            )

        with self.assertRaises(McpTransportError):
            await _transport(handler).list_tools()


class McpAdapterRealInvocationTests(unittest.IsolatedAsyncioTestCase):
    """McpAdapter: real transport turns an allowlisted server real; governance unchanged."""

    def _adapter(self, *, with_transport=True):
        transports = {"srv1": _transport()} if with_transport else {}
        return McpAdapter(
            enabled=True,
            allowed_servers=("srv1",),
            allowed_tools=("search",),
            server_trust={"srv1": "trusted"},
            transports=transports,
        )

    async def test_list_tools_discovers_real_normalized_tools(self):
        data = await _read(self._adapter(), "mcp.invoke", "list_tools", server="srv1")
        self.assertTrue(data["invoked"])
        self.assertEqual(data["tools"][0]["mcp_tool"], "search")
        self.assertEqual(data["tools"][0]["trust"], "trusted")

    async def test_invoke_calls_real_tool_and_returns_result(self):
        data = await _read(
            self._adapter(), "mcp.invoke", "invoke", server="srv1", mcp_tool="search", arguments={"query": "panda"}
        )
        self.assertTrue(data["invoked"])
        self.assertEqual(data["result"]["content"][0]["text"], "echo:panda")

    async def test_no_transport_preserves_prior_scaffold_only_behavior(self):
        # Regression guard: a server with no transport wired must behave
        # exactly as every pre-5.4 caller/test relies on (scaffold_only,
        # invoked=False) -- this is the default for every server unless a
        # transport is explicitly configured.
        data = await _read(
            self._adapter(with_transport=False), "mcp.invoke", "invoke", server="srv1", mcp_tool="search"
        )
        self.assertFalse(data["invoked"])
        self.assertEqual(data["note"], "scaffold_only")

    async def test_untrusted_server_denied_even_with_transport_wired(self):
        adapter = McpAdapter(
            enabled=True,
            allowed_servers=("srv1",),
            allowed_tools=("search",),
            server_trust={},  # srv1 present but not marked trusted
            transports={"srv1": _transport()},
        )
        with self.assertRaises(ToolPolicyDeniedError):
            await _read(adapter, "mcp.invoke", "invoke", server="srv1", mcp_tool="search")

    async def test_disallowed_server_denied_even_with_transport_wired(self):
        with self.assertRaises(ToolPolicyDeniedError):
            await _read(self._adapter(), "mcp.invoke", "invoke", server="evil", mcp_tool="search")

    async def test_tool_not_allowlisted_denied_even_with_transport_wired(self):
        with self.assertRaises(ToolPolicyDeniedError):
            await _read(self._adapter(), "mcp.invoke", "invoke", server="srv1", mcp_tool="delete_everything")

    async def test_transport_failure_fails_closed_not_silent(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        adapter = McpAdapter(
            enabled=True,
            allowed_servers=("srv1",),
            allowed_tools=("search",),
            server_trust={"srv1": "trusted"},
            transports={"srv1": _transport(handler)},
        )
        with self.assertRaises(ToolUnavailableError):
            await _read(adapter, "mcp.invoke", "invoke", server="srv1", mcp_tool="search")


class BootstrapMcpWiringTests(unittest.TestCase):
    """register_platform_tools -- env/param wiring into a real McpAdapter transport."""

    def test_server_urls_env_builds_real_transport(self):
        registry = ToolRegistry()
        out = register_platform_tools(
            registry,
            env={
                "TOOL_MCP_ENABLED": "true",
                "TOOL_MCP_ALLOWED_SERVERS": "srv1",
                "TOOL_MCP_TRUSTED_SERVERS": "srv1",
                "TOOL_MCP_SERVER_URLS": "srv1:https://mcp.fixture.test/rpc",
            },
        )
        mcp = out["adapters"]["mcp"]
        self.assertIn("srv1", mcp._transports)  # noqa: SLF001
        self.assertIsInstance(mcp._transports["srv1"], HttpMcpTransport)  # noqa: SLF001

    def test_direct_mcp_transports_param_is_wired(self):
        registry = ToolRegistry()
        injected = _transport()
        out = register_platform_tools(
            registry,
            env={"TOOL_MCP_ENABLED": "true", "TOOL_MCP_ALLOWED_SERVERS": "srv2", "TOOL_MCP_TRUSTED_SERVERS": "srv2"},
            mcp_transports={"srv2": injected},
        )
        mcp = out["adapters"]["mcp"]
        self.assertIs(mcp._transports["srv2"], injected)  # noqa: SLF001

    def test_default_no_env_no_param_preserves_empty_transports(self):
        # Regression guard: existing deployments with no MCP env configured
        # at all must see zero transports -- i.e. every allowed server
        # (if any) stays scaffold_only, exactly as before this activation.
        registry = ToolRegistry()
        out = register_platform_tools(registry)
        mcp = out["adapters"]["mcp"]
        self.assertEqual(mcp._transports, {})  # noqa: SLF001


class McpEndToEndGatewayTests(unittest.IsolatedAsyncioTestCase):
    """Full governed path: ToolRegistry -> ToolGateway -> McpAdapter -> real transport."""

    async def test_real_invocation_through_governed_gateway(self):
        registry = ToolRegistry()
        register_platform_tools(
            registry,
            env={
                "TOOL_MCP_ENABLED": "true",
                "TOOL_MCP_ALLOWED_SERVERS": "srv1",
                "TOOL_MCP_TRUSTED_SERVERS": "srv1",
            },
            mcp_transports={"srv1": _transport()},
        )
        gateway = ToolGateway(registry=registry, register_search=False)
        result = await gateway.invoke(
            ToolRequest(
                request_id="r1",
                workflow_id="wf",
                task_id="t",
                tenant_id="tenant-a",
                tool_id="mcp.invoke",
                operation="invoke",
                arguments={"server": "srv1", "mcp_tool": "search", "arguments": {"query": "panda"}},
                requested_capabilities=(CAP_MCP_INVOKE, CAP_EXTERNAL_READ),
            ),
            capabilities=CapabilitySet(
                subject_id="tenant-a", capabilities=(CAP_MCP_INVOKE, CAP_EXTERNAL_READ), issued_at=utc_now()
            ),
        )
        self.assertEqual(result.status, TOOL_STATUS_SUCCEEDED)
        self.assertTrue(result.success)
        self.assertTrue(result.data["invoked"])
        self.assertEqual(result.data["result"]["content"][0]["text"], "echo:panda")

    async def test_missing_mcp_capability_still_denied_on_real_path(self):
        registry = ToolRegistry()
        register_platform_tools(
            registry,
            env={
                "TOOL_MCP_ENABLED": "true",
                "TOOL_MCP_ALLOWED_SERVERS": "srv1",
                "TOOL_MCP_TRUSTED_SERVERS": "srv1",
            },
            mcp_transports={"srv1": _transport()},
        )
        gateway = ToolGateway(registry=registry, register_search=False)
        result = await gateway.invoke(
            ToolRequest(
                request_id="r2",
                workflow_id="wf",
                task_id="t",
                tenant_id="tenant-a",
                tool_id="mcp.invoke",
                operation="invoke",
                arguments={"server": "srv1", "mcp_tool": "search", "arguments": {"query": "panda"}},
                requested_capabilities=(CAP_EXTERNAL_READ,),
            ),
            capabilities=CapabilitySet(subject_id="tenant-a", capabilities=(CAP_EXTERNAL_READ,), issued_at=utc_now()),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_tool_capability")


if __name__ == "__main__":
    unittest.main()
