"""Thin deterministic contract adapters — scaffold/fake results, not vendor SDKs."""

from __future__ import annotations

from tools.errors import ToolPolicyDeniedError, ToolUnavailableError
from tools.models import ADAPTER_DEGRADED, ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE
from tools.platform.scaffold import ScaffoldAdapter


def _scaffold_payload(adapter: str, request, **extra) -> dict:
    return {
        "scaffold": True,
        "adapter": adapter,
        "tool_id": request.tool_id,
        "operation": request.operation,
        "provenance": {"adapter": adapter, "contract": True},
        **extra,
    }


class ContractReadAdapter(ScaffoldAdapter):
    """Enabled contract read adapter returning structured scaffold results."""

    def __init__(self, *, adapter_id: str, enabled: bool = True):
        super().__init__(adapter_id=adapter_id)
        self._enabled = enabled

    def health(self) -> str:
        return ADAPTER_HEALTHY if self._enabled else ADAPTER_UNAVAILABLE

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        return _scaffold_payload(self.adapter_id, request)


class ContractWriteAdapter(ScaffoldAdapter):
    """Write contract — raises unless used via SideEffect path / explicitly allowed."""

    def __init__(self, *, adapter_id: str, enabled: bool = True, allow_direct_write: bool = False):
        super().__init__(adapter_id=adapter_id)
        self._enabled = enabled
        self._allow_direct_write = allow_direct_write
        self.write_store: dict = {}

    def health(self) -> str:
        return ADAPTER_HEALTHY if self._enabled else ADAPTER_UNAVAILABLE

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        return _scaffold_payload(self.adapter_id, request, mode="read")

    async def execute_write(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        if not self._allow_direct_write and not (context or {}).get("via_side_effect"):
            raise ToolPolicyDeniedError("write_requires_side_effect_path")
        resource = str((request.arguments or {}).get("resource") or "default")
        value = (request.arguments or {}).get("value")
        self.write_store[resource] = value
        return _scaffold_payload(
            self.adapter_id, request, written=True, resource=resource, value=value
        )


class WebSearchContractAdapter(ContractReadAdapter):
    """May wrap SearchReadAdapter when a gateway provider is present."""

    def __init__(self, *, search_adapter=None, enabled: bool = True):
        super().__init__(adapter_id="web_search", enabled=enabled)
        self._search = search_adapter

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        if self._search is not None and hasattr(self._search, "execute_read"):
            data = await self._search.execute_read(request, context)
            if isinstance(data, dict):
                data = dict(data)
                data.setdefault("adapter", "web_search")
                data.setdefault("contract", True)
                return data
        return _scaffold_payload(
            "web_search",
            request,
            results=[],
            query=str((request.arguments or {}).get("query") or ""),
        )


class BrowserReadAdapter(ContractReadAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="browser", enabled=enabled)

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        if request.operation in {"write", "click", "type", "submit"}:
            raise ToolPolicyDeniedError("browser_write_not_on_read_adapter")
        return _scaffold_payload("browser", request, mode="read")


class BrowserWriteAdapter(ContractWriteAdapter):
    def __init__(self, *, enabled: bool = True, allow_direct_write: bool = False):
        super().__init__(
            adapter_id="browser_write",
            enabled=enabled,
            allow_direct_write=allow_direct_write,
        )


class FilesContractAdapter(ContractReadAdapter):
    def __init__(self, *, enabled: bool = True, filesystem_adapter=None):
        super().__init__(adapter_id="files", enabled=enabled)
        self._fs = filesystem_adapter

    async def execute_read(self, request, context) -> dict:
        if self._fs is not None and hasattr(self._fs, "execute_read"):
            return await self._fs.execute_read(request, context)
        return await super().execute_read(request, context)


class ExcelContractAdapter(ContractReadAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="excel", enabled=enabled)

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        op = request.operation
        if op == "inspect":
            return _scaffold_payload("excel", request, sheets=[], rows_estimate=0)
        if op == "read_range":
            return _scaffold_payload("excel", request, values=[], range=str((request.arguments or {}).get("range") or ""))
        raise ToolUnavailableError("excel_op_unavailable")

    async def execute_write(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        if not (context or {}).get("via_side_effect"):
            raise ToolPolicyDeniedError("write_requires_side_effect_path")
        return _scaffold_payload("excel", request, written=True)


class DocumentsOcrContractAdapter(ContractReadAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="documents", enabled=enabled)

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        return _scaffold_payload(
            "documents",
            request,
            text="",
            ocr=request.operation == "ocr",
        )


class EmailContractAdapter(ContractWriteAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="email", enabled=enabled, allow_direct_write=False)

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        if request.operation in {"search", "read"}:
            return _scaffold_payload("email", request, messages=[])
        raise ToolPolicyDeniedError("email_write_requires_side_effect")


class CalendarContractAdapter(ContractWriteAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="calendar", enabled=enabled)


class TelegramContractAdapter(ContractWriteAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="telegram", enabled=enabled)


class CrmContractAdapter(ContractWriteAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="crm", enabled=enabled)


class CmsBitrixContractAdapter(ContractWriteAdapter):
    def __init__(self, *, enabled: bool = True, bitrix=None):
        super().__init__(adapter_id="cms", enabled=enabled)
        self._bitrix = bitrix

    async def execute_read(self, request, context) -> dict:
        if self._bitrix is not None and hasattr(self._bitrix, "execute_read"):
            data = await self._bitrix.execute_read(request, context)
            if isinstance(data, dict):
                out = dict(data)
                out.setdefault("extends", "bitrix")
                return out
        return await super().execute_read(request, context)


class ExternalApiContractAdapter(ContractReadAdapter):
    """Allowlisted external API request foundation (1C / generic)."""

    def __init__(self, *, enabled: bool = True, allowed_hosts: tuple[str, ...] = ()):
        super().__init__(adapter_id="external_api", enabled=enabled)
        self._allowed_hosts = tuple(h.lower() for h in allowed_hosts)

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        host = str((request.arguments or {}).get("host") or "").lower()
        if self._allowed_hosts and host and host not in self._allowed_hosts:
            raise ToolPolicyDeniedError("host_not_allowlisted")
        return _scaffold_payload("external_api", request, host=host, allowlisted=True)


class DatabaseContractAdapter(ContractReadAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="database", enabled=enabled)

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        if request.operation not in {"select", "read", "query"}:
            raise ToolPolicyDeniedError("db_write_not_on_read_adapter")
        return _scaffold_payload("database", request, rows=[])


class ImageContractAdapter(ContractWriteAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="image", enabled=enabled)

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        return _scaffold_payload("image", request, mode=request.operation)


class ScrapingContractAdapter(ContractReadAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="scrape", enabled=enabled)

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        return _scaffold_payload(
            "scrape",
            request,
            url=str((request.arguments or {}).get("url") or ""),
            extracted={},
        )


class SeoAnalyticsContractAdapter(ContractWriteAdapter):
    def __init__(self, *, enabled: bool = True):
        super().__init__(adapter_id="seo", enabled=enabled)

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        if "write" in request.operation:
            raise ToolPolicyDeniedError("seo_write_requires_side_effect")
        return _scaffold_payload("seo", request, metrics={})


class McpAdapter(ScaffoldAdapter):
    """MCP bridge — server allowlist, trust, never Agent→MCP direct."""

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
        if not self._enabled:
            return ADAPTER_UNAVAILABLE
        if not self._allowed_servers:
            return ADAPTER_DEGRADED
        return ADAPTER_HEALTHY

    def register_normalized_tools(self, tools: list[dict]) -> list[dict]:
        """Normalize discovered MCP tools into internal metadata (no auto Core register)."""
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
            row = {
                "mcp_tool": name,
                "server": server,
                "trust": self._server_trust.get(server, "untrusted"),
                "input_schema": dict(item.get("input_schema") or {}),
            }
            out.append(row)
        self._normalized = out
        return out

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError("mcp_disabled")
        args = dict(request.arguments or {})
        server = str(args.get("server") or "").strip().lower()
        tool_name = str(args.get("mcp_tool") or args.get("tool") or "").strip()
        if not server or server not in self._allowed_servers:
            raise ToolPolicyDeniedError("untrusted_mcp_server")
        if not tool_name or tool_name.startswith("_"):
            raise ToolPolicyDeniedError("untrusted_mcp_tool")
        if self._allowed_tools and tool_name not in self._allowed_tools:
            raise ToolPolicyDeniedError("mcp_tool_not_allowlisted")
        trust = self._server_trust.get(server, "untrusted")
        if trust in {"untrusted", "UNTRUSTED"}:
            raise ToolPolicyDeniedError("untrusted_mcp_server")
        return _scaffold_payload(
            "mcp",
            request,
            server=server,
            mcp_tool=tool_name,
            trust=trust,
            invoked=False,
            note="scaffold_only",
        )

    async def execute_write(self, request, context) -> dict:
        raise ToolPolicyDeniedError("mcp_write_via_side_effect_only")


# Back-compat alias
McpScaffoldAdapter = McpAdapter
