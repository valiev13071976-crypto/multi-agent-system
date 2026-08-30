"""SideEffectExecutor bridge for commerce platform external writes (Block 11 P1-2)."""

from __future__ import annotations

import uuid

from commerce.capabilities import (
    CAP_CATALOG_WRITE,
    CAP_ORDER_WRITE,
    CAP_PRICING_PROPOSE,
    CAP_PRICING_WRITE,
    CAP_STOCK_WRITE,
)
from side_effects.errors import SideEffectExecutionError
from side_effects.models import (
    ADAPTER_RECON_UNKNOWN,
    AdapterExecutionResult,
    AdapterReconciliationResult,
    RollbackResult,
    SideEffectToolDescriptor,
)
from tools.errors import ToolAuthFailedError, ToolError, ToolPermanentFailureError
from tools.models import TOOL_TRUST_INTERNAL_SAFE, TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, ToolRequest

COMMERCE_RESOURCE_PREFIX = "commerce:"

COMMERCE_WRITE_TOOLS: tuple[dict, ...] = (
    {
        "tool_id": "commerce.price.apply",
        "operation": "apply_price",
        "capabilities": (CAP_PRICING_WRITE,),
        "external": True,
    },
    {
        "tool_id": "commerce.cms.product.create",
        "operation": "cms_create",
        "capabilities": (CAP_CATALOG_WRITE,),
        "external": True,
    },
    {
        "tool_id": "commerce.cms.stock.update",
        "operation": "cms_update_stock",
        "capabilities": (CAP_STOCK_WRITE,),
        "external": True,
    },
    {
        "tool_id": "commerce.cms.product.update",
        "operation": "cms_update_product",
        "capabilities": (CAP_CATALOG_WRITE,),
        "external": True,
    },
    {
        "tool_id": "commerce.cms.product.archive",
        "operation": "cms_archive_product",
        "capabilities": (CAP_CATALOG_WRITE,),
        "external": True,
    },
    {
        "tool_id": "commerce.product.import",
        "operation": "import",
        "capabilities": (CAP_CATALOG_WRITE,),
        "external": False,
    },
    {
        "tool_id": "commerce.price.decide",
        "operation": "decide_price",
        "capabilities": (CAP_PRICING_PROPOSE,),
        "external": False,
    },
    {
        "tool_id": "commerce.order.ingest",
        "operation": "ingest_order",
        "capabilities": (CAP_ORDER_WRITE,),
        "external": False,
    },
)


class CommercePlatformSideEffectAdapter:
    """Delegates governed commerce writes to ProductPlatformToolAdapter via SideEffectExecutor."""

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
            resource_prefix=COMMERCE_RESOURCE_PREFIX,
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
            arguments=payload,
            requested_capabilities=caps,
            idempotency_key=action.idempotency_key,
            tenant_id=tenant_id,
            actor_id="side_effect",
        )
        try:
            data = await self._platform.execute_write(
                request,
                {"via_side_effect": True, "now": context.stamp()},
            )
        except ToolAuthFailedError as exc:
            raise SideEffectExecutionError("side_effect_authorization_denied") from exc
        except ToolPermanentFailureError as exc:
            raise SideEffectExecutionError(str(exc.error_code or "commerce_execution_failed")) from exc
        except ToolError as exc:
            raise SideEffectExecutionError(str(getattr(exc, "error_code", "commerce_execution_failed"))) from exc
        external_ref = ""
        if isinstance(data, dict):
            external_ref = str(
                data.get("external_id")
                or data.get("receipt_id")
                or data.get("reservation_id")
                or data.get("order_id")
                or data.get("import_id")
                or data.get("decision_id")
                or ""
            )
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


def register_commerce_platform_side_effects(
    registry,
    platform_adapter,
    *,
    trust_level: str = TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    reversible: bool | None = None,
) -> tuple[str, ...]:
    """Register all production-reachable commerce write tools in SideEffectAdapterRegistry."""
    if platform_adapter is None:
        return ()
    registered: list[str] = []
    for spec in COMMERCE_WRITE_TOOLS:
        adapter = CommercePlatformSideEffectAdapter(
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
