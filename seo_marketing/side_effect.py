"""SideEffectExecutor bridge for SEO external writes (Block 12)."""

from __future__ import annotations

import uuid

from seo_marketing.capabilities import CAP_SEO_META_APPLY
from side_effects.errors import SideEffectExecutionError
from side_effects.models import (
    ADAPTER_RECON_UNKNOWN,
    AdapterExecutionResult,
    AdapterReconciliationResult,
    RollbackResult,
    SideEffectToolDescriptor,
)
from tools.errors import ToolAuthFailedError, ToolError, ToolPermanentFailureError
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, ToolRequest

SEO_RESOURCE_PREFIX = "seo:"

SEO_WRITE_TOOLS: tuple[dict, ...] = (
    {
        "tool_id": "seo.meta.apply",
        "operation": "meta_apply",
        "capabilities": (CAP_SEO_META_APPLY,),
        "external": True,
    },
    {
        "tool_id": "seo.metadata_write",
        "operation": "meta_apply",
        "capabilities": (CAP_SEO_META_APPLY,),
        "external": True,
    },
)


class SeoMarketingSideEffectAdapter:
    def __init__(
        self,
        *,
        tool_id: str,
        operation: str,
        capabilities_required: tuple[str, ...],
        platform_adapter,
        external: bool = True,
        trust_level: str = TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        reversible: bool | None = None,
    ):
        self._tool_id = tool_id
        self._operation = operation
        self._platform = platform_adapter
        self._external = external
        self._trust_level = trust_level
        self._reversible = reversible if reversible is not None else (not external)
        self._descriptor = SideEffectToolDescriptor(
            tool_id=tool_id,
            trust_level=trust_level,
            capabilities_required=tuple(capabilities_required),
            reversible=self._reversible,
            supports_idempotency=True,
            network_access=external,
            operations=(operation,),
            resource_prefix=SEO_RESOURCE_PREFIX,
            supports_reconciliation=False,
        )

    @property
    def descriptor(self) -> SideEffectToolDescriptor:
        return self._descriptor

    @property
    def tool_id(self) -> str:
        return self._tool_id

    @property
    def trust_level(self) -> str:
        return self._descriptor.trust_level

    @property
    def capabilities_required(self) -> tuple[str, ...]:
        return self._descriptor.capabilities_required

    @property
    def reversible(self) -> bool:
        return self._reversible

    async def execute(self, action, context) -> AdapterExecutionResult:
        payload = dict(getattr(context, "payload", {}) or {})
        tenant_id = getattr(context, "tenant_id", None) or str(payload.pop("_trusted_tenant_id", "") or "")
        if not tenant_id:
            raise SideEffectExecutionError("side_effect_tenant_required")
        caps = tuple(getattr(action, "requested_capabilities", None) or self.capabilities_required)
        request = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id=action.workflow_id,
            task_id=action.task_id,
            tool_id=self._tool_id,
            operation=action.operation or self._operation,
            arguments={
                **payload,
                "recommendation_id": payload.get("recommendation_id"),
                "idempotency_key": payload.get("idempotency_key") or action.idempotency_key,
            },
            requested_capabilities=caps,
            idempotency_key=action.idempotency_key,
            tenant_id=tenant_id,
            actor_id="side_effect",
        )
        try:
            data = await self._platform.execute_write(request, {"via_side_effect": True})
        except ToolAuthFailedError as exc:
            raise SideEffectExecutionError("side_effect_authorization_denied") from exc
        except ToolPermanentFailureError as exc:
            raise SideEffectExecutionError(str(exc.error_code or "seo_execution_failed")) from exc
        except ToolError as exc:
            raise SideEffectExecutionError(str(getattr(exc, "error_code", "seo_execution_failed"))) from exc
        external_ref = str((data or {}).get("external_ref") or "") if isinstance(data, dict) else ""
        return AdapterExecutionResult(
            success=True,
            external_reference=external_ref or f"{self._tool_id}:{action.action_id}",
            reversible=self.reversible,
            rollback_reference=None,
            metadata={"result": dict(data) if isinstance(data, dict) else {}},
        )

    async def rollback(self, result, context) -> RollbackResult:
        return RollbackResult(success=False, rollback_reference=None, metadata={"reason": "not_supported"})

    async def reconcile(self, execution_record, action, context) -> AdapterReconciliationResult:
        return AdapterReconciliationResult(status=ADAPTER_RECON_UNKNOWN)


def register_seo_marketing_side_effects(
    registry,
    platform_adapter,
    *,
    trust_level: str = TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    reversible: bool | None = None,
) -> tuple[str, ...]:
    if platform_adapter is None:
        return ()
    registered: list[str] = []
    for spec in SEO_WRITE_TOOLS:
        adapter = SeoMarketingSideEffectAdapter(
            tool_id=spec["tool_id"],
            operation=spec["operation"],
            capabilities_required=tuple(spec["capabilities"]),
            platform_adapter=platform_adapter,
            external=bool(spec.get("external", True)),
            trust_level=trust_level,
            reversible=reversible,
        )
        registry.register(adapter)
        registered.append(spec["tool_id"])
    return tuple(registered)
