"""Tool Platform adapter for Product Intelligence (Block 5.5).

All operations are internal data-processing/transformation over Panda's own
tenant-scoped product catalog plus the canonical ArtifactService -- never an
external system of record -- so, like ``data_intel``'s adapter, every
operation here is a governed *read*-classified tool (``_read_desc``, see
``tools/platform/descriptors.py``), not the write-execution
(autonomy-gate/HITL/idempotency-executor) pipeline reserved for
live-business-mutating actions (spec section 23).
"""

from __future__ import annotations

from product_intel.errors import ProductBatchRequired
from product_intel.planner import assert_sync_product_allowed
from tools.errors import ToolArgumentInvalidError, ToolNotFoundError


class ProductIntelToolAdapter:
    adapter_id = "product_intel"

    def __init__(self, service=None):
        self._svc = service

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("product.")

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._svc is not None else ADAPTER_UNAVAILABLE

    def _tenant(self, request) -> str:
        return str(request.tenant_id or "legacy-default")

    def _spreadsheet_refs(self, args: dict) -> list[dict]:
        refs = list(args.get("attachment_refs") or [])
        return [r for r in refs if isinstance(r, dict) and str(r.get("kind") or "") == "spreadsheet"]

    def _ingest_attachment(self, ref: dict, *, tenant: str) -> str:
        """Fetch trusted attachment bytes via ArtifactService and ingest them
        through the existing Block 5.1 ``DataIntelligenceService`` (attachment
        -> authorized source artifact -> dataset, never a second/parallel
        upload path) -- mirrors ``data_intel.tools.DataIntelToolAdapter``'s
        identically-named helper, which ``assist`` here must reuse rather than
        silently ignore a freshly attached price list (defect closure: a bare
        "Собери каталог товаров" with a NEW attachment previously fell
        through to a no-op status summary instead of importing it)."""

        data_svc = getattr(self._svc, "data_intelligence_service", None)
        artifact_service = getattr(self._svc, "artifact_service", None)
        if data_svc is None or artifact_service is None:
            raise ToolArgumentInvalidError("attachment_ingestion_unavailable")
        rec, blob = artifact_service.get_blob(
            tenant_id=tenant, artifact_id=str(ref.get("artifact_id") or "")
        )
        result = data_svc.ingest(
            blob,
            filename=rec.safe_filename or str(ref.get("filename") or "data.xlsx"),
            tenant_id=tenant,
        )
        return str(result["dataset_id"])

    async def execute_read(self, request, context) -> dict:
        if self._svc is None:
            raise ToolNotFoundError("tool_unavailable")
        args = dict(request.arguments or {})
        tenant = self._tenant(request)
        owner_id = str(getattr(request, "user_id", "") or "")
        op = request.operation

        if op == "import":
            rows = list(args.get("rows") or [])
            dataset_id = str(args.get("dataset_id") or "")
            source = str(args.get("source") or ("excel" if dataset_id else "payload"))
            bulk = bool(args.get("bulk"))
            try:
                if dataset_id and source == "excel":
                    result = self._svc.import_from_excel_dataset(
                        tenant_id=tenant,
                        dataset_id=dataset_id,
                        catalog_id=str(args.get("catalog_id") or "") or None,
                        owner_id=owner_id,
                        bulk=bulk,
                    )
                elif dataset_id and source == "acquisition":
                    result = self._svc.import_from_acquisition(
                        tenant_id=tenant,
                        records=list(args.get("records") or rows),
                        catalog_id=str(args.get("catalog_id") or "") or None,
                        owner_id=owner_id,
                        bulk=bulk,
                    )
                else:
                    if not rows:
                        raise ToolArgumentInvalidError("rows_or_dataset_id_required")
                    result = self._svc.import_rows(
                        tenant_id=tenant,
                        rows=rows,
                        source_type=source,
                        catalog_id=str(args.get("catalog_id") or "") or None,
                        owner_id=owner_id,
                        bulk=bulk,
                    )
            except ProductBatchRequired as exc:
                raise ToolArgumentInvalidError(str(exc.code)) from exc
            return {
                "import_id": result.import_id,
                "catalog_id": result.catalog_id,
                "total_rows": result.total_rows,
                "created": result.created,
                "updated": result.updated,
                "invalid": result.invalid,
                "ambiguous": result.ambiguous,
                "product_ids": list(result.product_ids),
            }

        if op == "match":
            outcome = self._svc.match_candidate(
                tenant_id=tenant,
                candidate=dict(args.get("candidate") or {}),
                catalog_id=str(args.get("catalog_id") or "") or None,
            )
            return {
                "state": outcome.state,
                "method": outcome.method,
                "matched_product_id": outcome.matched_product_id,
                "candidates": [
                    {"product_id": c.product_id, "confidence": c.confidence, "method": c.method}
                    for c in outcome.candidates
                ],
                "conflicts": list(outcome.conflicts),
            }

        if op == "duplicates":
            groups = self._svc.find_duplicate_groups(
                tenant_id=tenant, catalog_id=str(args.get("catalog_id") or "") or None
            )
            return {"groups": [{"product_ids": list(g.product_ids), "state": g.state} for g in groups]}

        if op == "validate":
            results = self._svc.validate_catalog(
                tenant_id=tenant, catalog_id=str(args.get("catalog_id") or "") or None
            )
            return {
                "results": {
                    pid: {"state": r.state, "issues": [i.code for i in r.issues]} for pid, r in results.items()
                },
                "summary": {
                    "valid": sum(1 for r in results.values() if r.state == "VALID"),
                    "warning": sum(1 for r in results.values() if r.state == "WARNING"),
                    "invalid": sum(1 for r in results.values() if r.state == "INVALID"),
                },
            }

        if op == "reconcile":
            return self._svc.reconcile_stock_from_dataset(
                tenant_id=tenant,
                dataset_id=str(args.get("dataset_id") or ""),
                catalog_id=str(args.get("catalog_id") or "") or None,
            )

        if op == "enrich":
            product_id = str(args.get("product_id") or "")
            if not product_id:
                raise ToolArgumentInvalidError("product_id_required")
            try:
                assert_sync_product_allowed(item_count=1, bulk=bool(args.get("bulk")))
            except ProductBatchRequired as exc:
                raise ToolArgumentInvalidError(str(exc.code)) from exc
            fields = tuple(args.get("fields") or ("description", "short_description", "seo_title", "seo_description"))
            return self._svc.enrich_product_content(
                tenant_id=tenant, product_id=product_id, fields=fields, channel=str(args.get("channel") or "marketplace")
            )

        if op == "associate_media":
            product_id = str(args.get("product_id") or "")
            if not product_id:
                raise ToolArgumentInvalidError("product_id_required")
            return self._svc.associate_media(
                tenant_id=tenant, product_id=product_id, artifact_ids=tuple(args.get("artifact_ids") or ())
            )

        if op == "export":
            return self._svc.export_catalog(
                tenant_id=tenant,
                catalog_id=str(args.get("catalog_id") or "") or None,
                as_artifact=bool(args.get("as_artifact")),
                owner_id=owner_id,
                conversation_id=str(args.get("conversation_id") or ""),
                request_id=str(getattr(request, "request_id", "") or ""),
            )

        if op == "assist":
            dataset_id = str(args.get("dataset_id") or "")
            if not dataset_id:
                sheets = self._spreadsheet_refs(args)
                if sheets:
                    dataset_id = self._ingest_attachment(sheets[0], tenant=tenant)
            try:
                return self._svc.execute_nl_request(
                    tenant_id=tenant,
                    text=str(args.get("text") or ""),
                    dataset_id=dataset_id,
                    catalog_id=str(args.get("catalog_id") or "") or None,
                )
            except ProductBatchRequired as exc:
                raise ToolArgumentInvalidError(str(exc.code)) from exc

        if op == "get":
            product = self._svc.get_product(str(args.get("product_id") or ""), tenant_id=tenant)
            if product is None:
                return {"found": False}
            return {"found": True, "product_id": product.product_id, "sku": product.sku, "title": product.title}

        raise ToolNotFoundError("operation_not_supported")
