"""Bounded tool adapters and descriptor factories for ToolGateway."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable

from autonomy.capabilities import (
    CAP_EXTERNAL_READ,
    CAP_EXTERNAL_WRITE,
    CAP_GITHUB_ISSUE_LABEL_WRITE,
)
from autonomy.models import ACTION_READ, ACTION_WRITE
from side_effects.github.models import GITHUB_OPERATIONS, GITHUB_TOOL_ID, RESOURCE_PREFIX
from side_effects.models import SideEffectToolDescriptor
from tools.models import (
    DEFAULT_SEARCH_TIMEOUT_SECONDS,
    SEARCH_OPERATION,
    SEARCH_TOOL_ID,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    ToolDescriptor,
)


@runtime_checkable
class ReadToolAdapter(Protocol):
    async def execute_read(self, request, context) -> dict: ...


def schema_hash_for(operations: tuple[str, ...], fields: tuple[str, ...] = ()) -> str:
    payload = {"operations": list(operations), "fields": list(fields)}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def search_tool_descriptor() -> ToolDescriptor:
    ops = (SEARCH_OPERATION,)
    return ToolDescriptor(
        tool_id=SEARCH_TOOL_ID,
        name="External Search",
        description="Read-only external evidence search",
        version="1.0.0",
        trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL,
        capabilities_required=(CAP_EXTERNAL_READ,),
        action_types_supported=(ACTION_READ,),
        operations=ops,
        read_only=True,
        reversible=True,
        idempotency_required=False,
        timeout_seconds=DEFAULT_SEARCH_TIMEOUT_SECONDS,
        enabled=True,
        network_access=True,
        resource_prefix="web:",
        schema_hash=schema_hash_for(ops, ("query", "max_results")),
    )


def github_issue_labels_descriptor(
    *, enabled: bool = False
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=GITHUB_TOOL_ID,
        name="GitHub Issue Labels",
        description="Bounded reversible GitHub issue label ensure/absent",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_WRITE, CAP_GITHUB_ISSUE_LABEL_WRITE),
        action_types_supported=(ACTION_WRITE,),
        operations=tuple(GITHUB_OPERATIONS),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=15.0,
        enabled=bool(enabled),
        network_access=True,
        resource_prefix=RESOURCE_PREFIX,
        schema_hash=schema_hash_for(
            tuple(GITHUB_OPERATIONS), ("resource", "idempotency_key")
        ),
    )


def descriptor_from_side_effect(
    se: SideEffectToolDescriptor,
    *,
    name: str | None = None,
    description: str = "",
    version: str = "1.0.0",
    enabled: bool = True,
    idempotency_required: bool | None = None,
    timeout_seconds: float = 15.0,
) -> ToolDescriptor:
    read_only = se.trust_level == TOOL_TRUST_READ_ONLY_EXTERNAL
    return ToolDescriptor(
        tool_id=se.tool_id,
        name=name or se.tool_id,
        description=description or se.tool_id,
        version=version,
        trust_level=se.trust_level,
        capabilities_required=tuple(se.capabilities_required),
        action_types_supported=(ACTION_READ if read_only else ACTION_WRITE,),
        operations=tuple(se.operations),
        read_only=read_only,
        reversible=bool(se.reversible),
        idempotency_required=(
            bool(se.supports_idempotency)
            if idempotency_required is None
            else bool(idempotency_required)
        ),
        timeout_seconds=float(timeout_seconds),
        enabled=enabled,
        network_access=bool(se.network_access),
        resource_prefix=str(se.resource_prefix or ""),
        schema_hash=schema_hash_for(tuple(se.operations)),
    )


class SearchReadAdapter:
    """Wraps ToolGateway.search for registry-backed invoke."""

    def __init__(self, gateway):
        self._gateway = gateway

    async def execute_read(self, request, context) -> dict:
        args = dict(request.arguments or {})
        query = str(args.get("query") or "")
        max_results = int(args.get("max_results") or 5)
        rows = await self._gateway.search(query, max_results=max_results)
        return {
            "results": [
                {
                    "title": row.title,
                    "url": row.url,
                    "snippet": row.snippet,
                    "source_domain": row.source_domain,
                    "trust_level": row.trust_level,
                }
                for row in rows
            ]
        }
