"""Tool descriptors for P17 procurement adapters."""

from __future__ import annotations

from autonomy.capabilities import CAP_EXTERNAL_READ
from autonomy.models import ACTION_READ
from procurement.adapters.models import (
    OP_CATALOG_READ,
    OP_RFQ_DRAFT,
    OP_SEARCH,
    PROCUREMENT_ADAPTER_SCHEMA_VERSION,
    TOOL_CATALOG_READ,
    TOOL_RFQ_DRAFT,
    TOOL_SUPPLIER_SEARCH,
)
from tools.adapters import schema_hash_for
from tools.models import (
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    ToolDescriptor,
)


def supplier_search_tool_descriptor(*, enabled: bool = True, timeout_seconds: float = 10.0) -> ToolDescriptor:
    ops = (OP_SEARCH,)
    return ToolDescriptor(
        tool_id=TOOL_SUPPLIER_SEARCH,
        name="Procurement Supplier Search",
        description="Bounded read-only external supplier discovery",
        version=PROCUREMENT_ADAPTER_SCHEMA_VERSION,
        trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL,
        capabilities_required=(CAP_EXTERNAL_READ,),
        action_types_supported=(ACTION_READ,),
        operations=ops,
        read_only=True,
        reversible=True,
        idempotency_required=False,
        timeout_seconds=float(timeout_seconds),
        enabled=bool(enabled),
        network_access=True,
        resource_prefix="procurement:supplier:",
        schema_hash=schema_hash_for(
            ops, ("product_name", "category", "country", "required_specs", "limit")
        ),
    )


def catalog_read_tool_descriptor(*, enabled: bool = True, timeout_seconds: float = 10.0) -> ToolDescriptor:
    ops = (OP_CATALOG_READ,)
    return ToolDescriptor(
        tool_id=TOOL_CATALOG_READ,
        name="Procurement Catalog Read",
        description="Bounded read of registered supplier catalog refs",
        version=PROCUREMENT_ADAPTER_SCHEMA_VERSION,
        trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL,
        capabilities_required=(CAP_EXTERNAL_READ,),
        action_types_supported=(ACTION_READ,),
        operations=ops,
        read_only=True,
        reversible=True,
        idempotency_required=False,
        timeout_seconds=float(timeout_seconds),
        enabled=bool(enabled),
        network_access=True,
        resource_prefix="procurement:catalog:",
        schema_hash=schema_hash_for(ops, ("supplier_ref", "catalog_ref", "limit")),
    )


def rfq_draft_tool_descriptor(*, enabled: bool = True, timeout_seconds: float = 5.0) -> ToolDescriptor:
    ops = (OP_RFQ_DRAFT,)
    return ToolDescriptor(
        tool_id=TOOL_RFQ_DRAFT,
        name="Procurement RFQ Draft",
        description="Local RFQ draft preparation — no external send",
        version=PROCUREMENT_ADAPTER_SCHEMA_VERSION,
        trust_level=TOOL_TRUST_INTERNAL_SAFE,
        capabilities_required=(),
        action_types_supported=(ACTION_READ,),
        operations=ops,
        read_only=True,
        reversible=True,
        idempotency_required=False,
        timeout_seconds=float(timeout_seconds),
        enabled=bool(enabled),
        network_access=False,
        resource_prefix="procurement:rfq:",
        schema_hash=schema_hash_for(
            ops,
            (
                "request_id",
                "supplier_ref",
                "item_name",
                "quantity",
                "unit",
                "specs",
                "deadline",
                "questions",
                "language",
            ),
        ),
    )


def procurement_adapter_schema_snapshot() -> dict:
    return {
        "procurement_adapter_schema_version": PROCUREMENT_ADAPTER_SCHEMA_VERSION,
        "tools": [
            supplier_search_tool_descriptor().tool_id,
            catalog_read_tool_descriptor().tool_id,
            rfq_draft_tool_descriptor().tool_id,
        ],
        "forbidden_args": [
            "base_url",
            "headers",
            "auth",
            "timeout",
            "method",
            "raw_url",
            "shell",
            "command",
        ],
        "rfq_external_send": False,
        "purchase_execution": False,
    }
