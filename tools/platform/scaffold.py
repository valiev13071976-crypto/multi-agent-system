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
    def __init__(self):
        super().__init__(adapter_id="mcp")

    async def execute_read(self, request, context) -> dict:
        args = dict(request.arguments or {})
        tool_name = str(args.get("mcp_tool") or "")
        if not tool_name or tool_name.startswith("_"):
            from tools.errors import ToolPolicyDeniedError

            raise ToolPolicyDeniedError("untrusted_mcp_tool")
        raise ToolUnavailableError("mcp_disabled")


class CmsScaffoldAdapter(ScaffoldAdapter):
    def __init__(self, *, adapter_id: str = "cms"):
        super().__init__(adapter_id=adapter_id)


class BitrixAdapter(CmsScaffoldAdapter):
    """Bitrix HTTP foundation — uses integration config when enabled."""

    def __init__(self, *, credential_store=None, enabled: bool = False):
        super().__init__(adapter_id="bitrix")
        self._credentials = credential_store
        self._enabled = enabled

    def health(self) -> str:
        from tools.models import ADAPTER_DEGRADED, ADAPTER_HEALTHY

        if not self._enabled:
            return ADAPTER_UNAVAILABLE
        return ADAPTER_DEGRADED if self._credentials is None else ADAPTER_HEALTHY

    async def execute_read(self, request, context) -> dict:
        if request.operation == "product_read":
            return {
                "scaffold": True,
                "adapter": "bitrix",
                "operation": request.operation,
                "provenance": {"integration": "bitrix"},
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
