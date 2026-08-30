"""Tool Platform adapter for Data Intelligence."""

from __future__ import annotations

import base64

from data_intel.errors import DataIntelError
from tools.errors import ToolArgumentInvalidError, ToolError, ToolNotFoundError


class DataIntelToolAdapter:
    adapter_id = "data_intel"

    def __init__(self, service=None):
        self._svc = service

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("data.")

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._svc is not None else ADAPTER_UNAVAILABLE

    def _tenant(self, request) -> str:
        return str(request.tenant_id or "legacy-default")

    async def execute_read(self, request, context) -> dict:
        if self._svc is None:
            raise ToolNotFoundError("tool_unavailable")
        args = dict(request.arguments or {})
        op = request.operation
        tenant = self._tenant(request)
        try:
            if op == "ingest":
                raw = args.get("content_b64")
                if not raw:
                    raise ToolArgumentInvalidError()
                data = base64.b64decode(raw)
                row_hint = args.get("row_count")
                if row_hint is not None:
                    from data_intel.planner import assert_sync_data_allowed

                    assert_sync_data_allowed(
                        row_count=int(row_hint),
                        byte_size=len(data),
                        operations=("ingest",),
                    )
                return self._svc.ingest(
                    data,
                    filename=str(args.get("filename") or "data.csv"),
                    tenant_id=tenant,
                    source_document_id=str(args.get("source_document_id") or ""),
                )
            if op == "duplicates":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return {
                    "groups": self._svc.duplicates(
                        ds,
                        tenant_id=tenant,
                        business_keys=list(args.get("business_keys") or []),
                    )
                }
            if op == "merge":
                left = list(args.get("left_rows") or [])
                right = list(args.get("right_rows") or [])
                return self._svc.merge(
                    left,
                    right,
                    keys=list(args.get("keys") or []),
                    how=str(args.get("how") or "inner"),
                )
            if op == "profile":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return self._svc.profile(ds, tenant_id=tenant)
            if op == "normalize":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return self._svc.normalize(ds, tenant_id=tenant)
            if op == "search":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return self._svc.search(
                    ds,
                    tenant_id=tenant,
                    inn=args.get("inn"),
                    company_name=args.get("company_name"),
                    sku=args.get("sku"),
                    ean=args.get("ean"),
                    article=args.get("article"),
                    document_number=args.get("document_number"),
                    amount=args.get("amount"),
                    date=args.get("date"),
                    filters=args.get("filters"),
                    fuzzy_name=bool(args.get("fuzzy_name")),
                    sort_by=args.get("sort_by"),
                    sort_desc=bool(args.get("sort_desc")),
                    offset=int(args.get("offset") or 0),
                    limit=int(args.get("limit") or 100),
                )
            if op == "match":
                return self._svc.match(
                    dict(args.get("left") or {}),
                    dict(args.get("right") or {}),
                    entity_type=str(args.get("entity_type") or "counterparty"),
                )
            if op == "compare":
                return self._svc.compare_prices(
                    list(args.get("left_rows") or []),
                    list(args.get("right_rows") or []),
                    left_supplier=str(args.get("left_supplier") or "left"),
                    right_supplier=str(args.get("right_supplier") or "right"),
                )
            if op == "reconcile":
                return self._svc.reconcile(
                    str(args.get("kind") or "payment"),
                    list(args.get("left_rows") or []),
                    list(args.get("right_rows") or []),
                )
            if op == "aggregate":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return {
                    "rows": self._svc.aggregate(
                        ds,
                        tenant_id=tenant,
                        group_by=list(args.get("group_by") or []),
                        measures=dict(args.get("measures") or {"_rows": "count"}),
                    )
                }
            if op == "generate_excel":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                result = self._svc.generate_excel(
                    ds,
                    tenant_id=tenant,
                    kind=str(args.get("kind") or "data"),
                    comparison=args.get("comparison"),
                )
                content = result.pop("content", b"")
                return {
                    **{k: v for k, v in result.items() if k != "content"},
                    "content_b64": base64.b64encode(content).decode("ascii") if content else "",
                }
            raise ToolArgumentInvalidError()
        except DataIntelError as exc:
            raise ToolError(exc.reason) from exc
