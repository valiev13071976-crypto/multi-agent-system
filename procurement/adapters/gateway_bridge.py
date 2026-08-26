"""Sync bridge helper: ProcurementService → ToolGateway.invoke (no direct adapter calls)."""

from __future__ import annotations

import asyncio
import uuid

from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
from autonomy.models import utc_now
from procurement.errors import (
    PROCUREMENT_CATALOG_FETCH_FAILED,
    PROCUREMENT_CATALOG_REF_INVALID,
    PROCUREMENT_EXTERNAL_QUERY_INVALID,
    PROCUREMENT_EXTERNAL_RATE_LIMITED,
    PROCUREMENT_EXTERNAL_SEARCH_DISABLED,
    PROCUREMENT_EXTERNAL_SOURCE_DENIED,
    PROCUREMENT_EXTERNAL_TIMEOUT,
    PROCUREMENT_RFQ_DRAFT_INVALID,
    PROCUREMENT_RFQ_SEND_UNSUPPORTED,
    ProcurementError,
)
from tools.models import ToolRequest

_ERROR_MAP = {
    "tool_timeout": PROCUREMENT_EXTERNAL_TIMEOUT,
    "tool_disabled": PROCUREMENT_EXTERNAL_SEARCH_DISABLED,
    "tool_argument_invalid": PROCUREMENT_EXTERNAL_QUERY_INVALID,
    "tool_policy_denied": PROCUREMENT_EXTERNAL_SOURCE_DENIED,
    PROCUREMENT_EXTERNAL_SEARCH_DISABLED: PROCUREMENT_EXTERNAL_SEARCH_DISABLED,
    PROCUREMENT_EXTERNAL_SOURCE_DENIED: PROCUREMENT_EXTERNAL_SOURCE_DENIED,
    PROCUREMENT_EXTERNAL_QUERY_INVALID: PROCUREMENT_EXTERNAL_QUERY_INVALID,
    PROCUREMENT_EXTERNAL_TIMEOUT: PROCUREMENT_EXTERNAL_TIMEOUT,
    PROCUREMENT_EXTERNAL_RATE_LIMITED: PROCUREMENT_EXTERNAL_RATE_LIMITED,
    PROCUREMENT_CATALOG_REF_INVALID: PROCUREMENT_CATALOG_REF_INVALID,
    PROCUREMENT_CATALOG_FETCH_FAILED: PROCUREMENT_CATALOG_FETCH_FAILED,
    PROCUREMENT_RFQ_DRAFT_INVALID: PROCUREMENT_RFQ_DRAFT_INVALID,
    PROCUREMENT_RFQ_SEND_UNSUPPORTED: PROCUREMENT_RFQ_SEND_UNSUPPORTED,
}


def invoke_tool_sync(tool_gateway, *, tool_id: str, operation: str, arguments: dict, capabilities=None):
    """Invoke a registered tool via ToolGateway only. Raises ProcurementError on failure."""

    if tool_gateway is None:
        raise ProcurementError(
            PROCUREMENT_EXTERNAL_SOURCE_DENIED, details={"reason": "gateway_unavailable"}
        )
    caps = capabilities
    if caps is None:
        if tool_id != "procurement.rfq_draft":
            caps = CapabilitySet(
                subject_id="procurement_service",
                capabilities=(CAP_EXTERNAL_READ,),
                issued_at=utc_now(),
            )
    request = ToolRequest(
        request_id=str(uuid.uuid4()),
        workflow_id="procurement",
        task_id="procurement",
        tool_id=tool_id,
        operation=operation,
        arguments=dict(arguments or {}),
        requested_capabilities=tuple(getattr(caps, "capabilities", ()) or ()),
    )

    async def _run():
        return await tool_gateway.invoke(request, capabilities=caps)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        result = asyncio.run(_run())
    else:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(asyncio.run, _run()).result(timeout=60)

    if getattr(result, "success", False):
        return result
    code = str(getattr(result, "error_code", "") or "tool_execution_failed")
    mapped = _ERROR_MAP.get(code)
    if mapped is None and tool_id == "procurement.catalog_read":
        mapped = PROCUREMENT_CATALOG_FETCH_FAILED
    elif mapped is None and tool_id == "procurement.rfq_draft":
        mapped = PROCUREMENT_RFQ_DRAFT_INVALID
    elif mapped is None:
        mapped = PROCUREMENT_EXTERNAL_SOURCE_DENIED
    raise ProcurementError(mapped, details={"tool_error": code})
