"""Register Tool Platform adapters into ToolRegistry."""

from __future__ import annotations

import os
from pathlib import Path

from tools.integration import IntegrationCredentialStore
from tools.platform.descriptors import (
    aspro_descriptor,
    bitrix_descriptor,
    browser_descriptor,
    calendar_descriptor,
    crm_descriptor,
    document_parse_descriptor,
    document_search_descriptor,
    email_descriptor,
    filesystem_read_descriptor,
    filesystem_write_descriptor,
    http_request_descriptor,
    marketplace_descriptor,
    mcp_descriptor,
    onec_descriptor,
    sql_query_descriptor,
    telegram_descriptor,
    terminal_descriptor,
    cms_descriptor,
)
from tools.platform.documents import DocumentToolAdapter
from tools.platform.filesystem import FilesystemAdapter
from tools.platform.http_adapter import HttpAdapter
from tools.platform.scaffold import (
    AsproAdapter,
    BitrixAdapter,
    CmsScaffoldAdapter,
    McpScaffoldAdapter,
    ScaffoldAdapter,
    TerminalScaffoldAdapter,
)
from tools.registry import ToolRegistry


def _workspace_roots(env: dict | None = None) -> tuple[str, ...]:
    source = env if env is not None else os.environ
    raw = (source.get("TOOL_FS_ALLOWED_ROOTS") or "").strip()
    if raw:
        return tuple(p.strip() for p in raw.split(";") if p.strip())
    return (str(Path.cwd()),)


def _allowed_http_hosts(env: dict | None = None) -> tuple[str, ...]:
    source = env if env is not None else os.environ
    raw = (source.get("TOOL_HTTP_ALLOWED_HOSTS") or "").strip()
    if raw:
        return tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return ()


def register_platform_tools(
    registry: ToolRegistry,
    *,
    env: dict | None = None,
    document_service=None,
    credential_store: IntegrationCredentialStore | None = None,
) -> dict:
    """Register platform adapters. Returns adapter map for health wiring."""
    creds = credential_store or IntegrationCredentialStore()
    fs = FilesystemAdapter(allowed_roots=_workspace_roots(env))
    http = HttpAdapter(
        allowed_hosts=_allowed_http_hosts(env),
        credential_store=creds,
    )
    doc = DocumentToolAdapter(document_service)
    terminal_enabled = (env or os.environ).get("TOOL_TERMINAL_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
    }
    terminal = TerminalScaffoldAdapter(enabled=terminal_enabled)
    mcp = McpScaffoldAdapter()
    bitrix_enabled = (env or os.environ).get("BITRIX_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
    }
    bitrix = BitrixAdapter(credential_store=creds, enabled=bitrix_enabled)
    aspro = AsproAdapter(bitrix=bitrix, enabled=bitrix_enabled)

    registrations = [
        (filesystem_read_descriptor(enabled=bool(fs._roots)), fs),
        (filesystem_write_descriptor(enabled=False), fs),
        (http_request_descriptor(enabled=bool(http._allowed_hosts)), http),
        (terminal_descriptor(enabled=terminal_enabled), terminal),
        (browser_descriptor(enabled=False), ScaffoldAdapter(adapter_id="browser")),
        (document_parse_descriptor(enabled=document_service is not None), doc),
        (document_search_descriptor(enabled=document_service is not None), doc),
        (mcp_descriptor(enabled=False), mcp),
        (cms_descriptor(enabled=False), CmsScaffoldAdapter(adapter_id="cms")),
        (bitrix_descriptor(enabled=bitrix_enabled), bitrix),
        (aspro_descriptor(enabled=bitrix_enabled), aspro),
        (telegram_descriptor(enabled=False), ScaffoldAdapter(adapter_id="telegram")),
        (crm_descriptor(enabled=False), ScaffoldAdapter(adapter_id="crm")),
        (onec_descriptor(enabled=False), ScaffoldAdapter(adapter_id="onec")),
        (marketplace_descriptor(enabled=False), ScaffoldAdapter(adapter_id="marketplace")),
        (sql_query_descriptor(enabled=False), ScaffoldAdapter(adapter_id="sql")),
        (email_descriptor(enabled=False), ScaffoldAdapter(adapter_id="email")),
        (calendar_descriptor(enabled=False), ScaffoldAdapter(adapter_id="calendar")),
    ]
    adapters: dict = {}
    for desc, adapter in registrations:
        registry.register(desc, adapter=adapter)
        adapters[desc.adapter_id] = adapter
    return {"adapters": adapters, "credential_store": creds}
