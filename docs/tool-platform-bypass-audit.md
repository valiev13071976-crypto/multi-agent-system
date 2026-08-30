# Tool Platform — Legitimate Low-Level Paths (Bypass Audit)

This note lists **legitimate** low-level call sites that are **not** Core
ToolGateway bypasses. Agents and Workflow Core must still go through
`ToolRegistry → ToolRouter → permissions → ToolGateway` (writes →
`SideEffectExecutor`).

## Legitimate paths

| Path | Why it is not a Core bypass |
|------|------------------------------|
| `SearchProvider` / `ToolGateway.search()` | Legacy READ_ONLY_EXTERNAL evidence path owned by ToolGateway; not an Agent→vendor SDK hop. |
| `SideEffectAdapter.execute` / `execute_write` | Invoked only by `SideEffectExecutor` after gate/HITL/idempotency — the write half of the gateway contract. |
| `HttpAdapter` / `FilesystemAdapter` | Registered adapters reached only via `ToolGateway.invoke` → registry adapter lookup. |
| `McpAdapter.execute_read` | Scaffold/allowlisted MCP bridge behind gateway; Agent→MCP direct is forbidden. |
| Contract/Fake adapters under `tools/platform/` | Test and foundation scaffolds; production vendors remain `enabled=False` until wired. |

## Not allowed

- Agent or Workflow step calling vendor SDKs (OpenAI tools, Anthropic tools, MCP clients, Bitrix HTTP) **directly**
- Redefining tool governance metadata from user/tool payload
- Silent unauthorized fallback to another tool when capability/trust/tenant checks fail

## Codebase scan notes

- Model provider HTTP clients (`agents/anthropic_agent.py`, etc.) are **LLM inference**, not Tool Platform adapters.
- No Agent→MCP direct invoke path was found; MCP is registry-backed (`mcp.invoke`) and fail-closed without allowlist/trust.
