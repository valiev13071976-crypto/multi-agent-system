"""Scaffold adapters — contract-only until credentials/provider wired."""

from __future__ import annotations

from tools.errors import ToolUnavailableError
from tools.models import ADAPTER_UNAVAILABLE, TOOL_STATUS_FAILED, ToolResult


class ScaffoldAdapter:
    """Returns tool_unavailable unless subclass overrides."""

    adapter_id: str = "scaffold"

    def __init__(self, *, adapter_id: str):
        self.adapter_id = adapter_id

    def supports(self, tool_id: str) -> bool:
        return tool_id.split(".", 1)[0] == self.adapter_id or tool_id.startswith(
            f"{self.adapter_id}."
        )

    def health(self) -> str:
        return ADAPTER_UNAVAILABLE

    async def execute_read(self, request, context) -> dict:
        raise ToolUnavailableError()

    async def execute_write(self, request, context) -> dict:
        raise ToolUnavailableError()


class TerminalScaffoldAdapter(ScaffoldAdapter):
    def __init__(self, *, enabled: bool = False):
        super().__init__(adapter_id="terminal")
        self._enabled = enabled

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY

        return ADAPTER_HEALTHY if self._enabled else ADAPTER_UNAVAILABLE

    async def execute_write(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        raise ToolUnavailableError("terminal_not_configured")


class McpScaffoldAdapter(ScaffoldAdapter):
    """Legacy scaffold — prefer tools.platform.contracts.McpAdapter."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        allowed_servers: tuple[str, ...] = (),
        allowed_tools: tuple[str, ...] = (),
        server_trust: dict[str, str] | None = None,
    ):
        super().__init__(adapter_id="mcp")
        self._enabled = enabled
        self._allowed_servers = frozenset(s.lower() for s in allowed_servers)
        self._allowed_tools = frozenset(allowed_tools)
        self._server_trust = dict(server_trust or {})
        self._normalized: list[dict] = []

    def health(self) -> str:
        from tools.models import ADAPTER_DEGRADED, ADAPTER_HEALTHY

        if not self._enabled:
            return ADAPTER_UNAVAILABLE
        if not self._allowed_servers:
            return ADAPTER_DEGRADED
        return ADAPTER_HEALTHY

    def register_normalized_tools(self, tools: list[dict]) -> list[dict]:
        out = []
        for item in tools or []:
            name = str(item.get("name") or item.get("tool") or "").strip()
            server = str(item.get("server") or "").strip().lower()
            if not name or name.startswith("_"):
                continue
            if self._allowed_servers and server not in self._allowed_servers:
                continue
            if self._allowed_tools and name not in self._allowed_tools:
                continue
            out.append(
                {
                    "mcp_tool": name,
                    "server": server,
                    "trust": self._server_trust.get(server, "untrusted"),
                    "input_schema": dict(item.get("input_schema") or {}),
                }
            )
        self._normalized = out
        return out

    async def execute_read(self, request, context) -> dict:
        args = dict(request.arguments or {})
        tool_name = str(args.get("mcp_tool") or args.get("tool") or "")
        server = str(args.get("server") or "").strip().lower()
        if not tool_name or tool_name.startswith("_"):
            from tools.errors import ToolPolicyDeniedError

            raise ToolPolicyDeniedError("untrusted_mcp_tool")
        if self._allowed_servers:
            if not server or server not in self._allowed_servers:
                from tools.errors import ToolPolicyDeniedError

                raise ToolPolicyDeniedError("untrusted_mcp_server")
        if not self._enabled:
            raise ToolUnavailableError("mcp_disabled")
        trust = self._server_trust.get(server, "untrusted")
        if trust in {"untrusted", "UNTRUSTED"} and self._allowed_servers:
            from tools.errors import ToolPolicyDeniedError

            raise ToolPolicyDeniedError("untrusted_mcp_server")
        return {
            "scaffold": True,
            "adapter": "mcp",
            "server": server,
            "mcp_tool": tool_name,
            "trust": trust,
            "provenance": {"adapter": "mcp", "contract": True},
        }


class CmsScaffoldAdapter(ScaffoldAdapter):
    def __init__(self, *, adapter_id: str = "cms"):
        super().__init__(adapter_id=adapter_id)


class BitrixAdapter(CmsScaffoldAdapter):
    """Bitrix HTTP foundation — uses integration credential bridge when enabled."""

    def __init__(self, *, credential_store=None, enabled: bool = False, integration_service=None):
        super().__init__(adapter_id="bitrix")
        self._credentials = credential_store
        self._enabled = enabled
        self._integration_service = integration_service

    def health(self) -> str:
        from tools.models import ADAPTER_DEGRADED, ADAPTER_HEALTHY

        if not self._enabled:
            return ADAPTER_UNAVAILABLE
        if self._integration_service is not None or self._credentials is not None:
            return ADAPTER_HEALTHY
        return ADAPTER_DEGRADED

    async def execute_read(self, request, context) -> dict:
        if request.operation == "product_read":
            # Credentials presence does not grant permission; scaffold read only.
            integration_id = str((request.arguments or {}).get("integration_id") or "")
            if integration_id and self._credentials and request.tenant_id:
                self._credentials.assert_tenant_access(request.tenant_id, integration_id)
            return {
                "scaffold": True,
                "adapter": "bitrix",
                "operation": request.operation,
                "provenance": {"integration": "bitrix"},
                "auth": "server_side_ref_only",
            }
        raise ToolUnavailableError()


class AsproAdapter(BitrixAdapter):
    """Thin specialization — delegates to Bitrix contract."""

    def __init__(self, *, bitrix: BitrixAdapter | None = None, enabled: bool = False):
        super().__init__(credential_store=getattr(bitrix, "_credentials", None), enabled=enabled)
        self.adapter_id = "aspro"
        self._bitrix = bitrix

    async def execute_read(self, request, context) -> dict:
        data = await super().execute_read(request, context)
        data["extends"] = "bitrix"
        data["adapter"] = "aspro"
        return data
