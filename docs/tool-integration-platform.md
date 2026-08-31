# Tool & Integration Platform

## Architecture

Governed external actions flow through one canonical path:

```
Agent / Workflow / API
        ↓
UnifiedToolExecutor (optional trusted entry)
        ↓
Tool Registry
        ↓
Tool Router
        ↓
Tool Gateway (policy + approval + idempotency)
        ↓
Read adapter OR SideEffectExecutor (writes)
        ↓
External system
        ↓
Normalized ToolResult + telemetry/audit
```

**MCP** is an adapter family (`ADAPTER_MCP`). MCP tools register into the same registry and pass through the same gateway. MCP is not the platform core.

## Core contracts

| Concept | Module | Type |
|---------|--------|------|
| Tool definition | `tools/models.py` | `ToolDescriptor` |
| Invocation | `tools/invocation.py` | `ToolInvocation` |
| Request | `tools/models.py` | `ToolRequest` |
| Result | `tools/models.py` | `ToolResult` |
| Context | `workflow/run_envelope.py` | `RunEnvelope` |
| Side-effect semantics | `tools/side_effect_semantics.py` | `READ_ONLY` … `FINANCIAL_OR_HIGH_RISK` |
| Integration families | `tools/integration_families.py` | `ADAPTER_*` tokens |
| Errors | `tools/errors.py` + `tools/failure.py` | normalized taxonomy |

## Side-effect model

Semantic levels (governance-facing):

- `READ_ONLY` — read/search/inspect; no external mutation
- `WRITE_REVERSIBLE` — governed reversible writes
- `WRITE_EXTERNAL` — external side effects
- `DESTRUCTIVE` — fail-closed without explicit approval policy
- `FINANCIAL_OR_HIGH_RISK` — fail-closed without explicit approval policy

`ToolGateway.invoke()` calls `enforce_side_effect_policy()` before execution.

## Capability & tenant governance

- Capabilities: `autonomy/capabilities.py` + `tools/permissions.py`
- Tenant context: from trusted `RunEnvelope` / `ToolRequest` (never from raw user payload overrides)
- Approvals: existing `HITLService` + `SideEffectExecutor` for writes
- Secrets: `tools/secrets_ref.py` — references only, never plaintext in registry

## Interactive vs batch

Set `workload_class_hint` on `ToolDescriptor` (`interactive`, `batch`, `background`). Hints integrate with existing queue/runtime classification via `tools/workload_hints.py`.

## How to add a new tool / integration

1. **Define contract** — create a `ToolDescriptor` factory with `tool_id`, `version`, capabilities, side-effect level, schemas in `metadata.input_schema`, adapter family in `metadata.adapter_type`.
2. **Implement adapter** — implement `ReadToolAdapter.execute_read` or register a `SideEffectAdapter` for writes (`tools/adapter_protocol.py`).
3. **Declare governance** — set `capabilities_required`, `operation_class`, `approval_policy`, `tenant_scope_policy`, `idempotency_required`.
4. **Register** — `ToolRegistry.register(descriptor, adapter=...)` before freeze (see `side_effects/runtime.py`, `tools/platform/bootstrap.py`).
5. **Add contract tests** — subclass `AdapterContractTestCase` from `tools/contract_kit.py` and extend `tests/test_tool_integration_platform.py`.
6. **Verify gateway policy** — ensure invoke path succeeds only with correct capabilities and fails closed on missing approval/tenant/scope.
7. **Enable** — set `enabled=True` only after adapter + credentials are configured.

## Reference tests

- `tests/test_tool_integration_platform.py` — platform acceptance (registry/router/gateway/contracts)
- `tests/test_tool_integration_platform_expansion_closure.py` — applied expansion closure (semantics, negative paths, contract kit)

## Bypass audit

See `docs/tool-platform-bypass-audit.md` for forbidden direct-adapter call sites.
